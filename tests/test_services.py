import copy
import hashlib
import hmac
import json
import threading
from dataclasses import replace
from http.client import HTTPConnection
from types import SimpleNamespace

import pytest
import yaml

from cirra.domain import CirraError, canonical
from cirra.job_queue import Queue
from cirra.service_cli import credential_file
from cirra.service_config import ServiceConfig, load_service
from cirra.webhook import WebhookServer, accept
from cirra.worker import work_once

SECRET = "independent-webhook-secret-32-characters"


@pytest.fixture
def route(inputs):
    _, config = inputs()
    config = replace(
        config,
        repositories=(
            {"provider": "github", "instance": "https://github.com", "repository_id": "42"},
        ),
    )
    return SimpleNamespace(
        name="test",
        provider="github",
        instance="https://github.com",
        repository="example/repo",
        repository_id="42",
        policy=config,
        secret_env="HOOK_SECRET",
        digest="policy-v1",
    )


def delivery(action="opened", identity="delivery-1", **extra):
    raw = canonical(
        {
            "action": action,
            "repository": {"id": 42, "full_name": "example/repo"},
            "pull_request": {"number": 1, "head": {"sha": "do-not-trust"}},
            **extra,
        }
    ).encode()
    return raw, {
        "X-GitHub-Delivery": identity,
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": "sha256="
        + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest(),
        "Content-Type": "application/json",
    }


def settings(route, **values):
    return ServiceConfig(
        routes={route.name: route},
        poll_seconds=0.1,
        retry_seconds=1,
        max_attempts=values.get("max_attempts", 3),
        max_jobs=values.get("max_jobs", 100),
    )


def test_authenticated_queue_is_durable_and_deduplicates_body_not_just_header(route, tmp_path):
    raw, headers = delivery()
    result = accept(route, raw, headers, SECRET, Queue(tmp_path))
    assert result["state"] == "accepted"
    headers["X-GitHub-Delivery"] = "another-header"
    assert accept(route, raw, headers, SECRET, Queue(tmp_path))["duplicate"]
    pending = Queue(tmp_path).pending()
    assert len(pending) == 1 and pending[0]["hint"]["change"] == 1
    assert "do-not-trust" not in canonical(pending)
    assert SECRET not in canonical(pending)


def test_authentication_route_action_and_capacity_guards(route, tmp_path):
    queue = Queue(tmp_path, 1)
    raw, headers = delivery()
    with pytest.raises(CirraError, match="authentication"):
        accept(route, raw + b" ", headers, SECRET, queue)
    with pytest.raises(CirraError, match="route"):
        accept(
            SimpleNamespace(**{**vars(route), "repository": "other/repo"}),
            raw,
            headers,
            SECRET,
            queue,
        )
    closed, closed_headers = delivery("closed")
    assert accept(route, closed, closed_headers, SECRET, queue)["state"] == "ignored"
    assert not queue.pending()
    accept(route, raw, headers, SECRET, queue)
    changed, changed_headers = delivery("synchronize")
    with pytest.raises(CirraError, match="reused"):
        accept(route, changed, changed_headers, SECRET, queue)
    changed_headers["X-GitHub-Delivery"] = "delivery-2"
    with pytest.raises(CirraError, match="capacity"):
        accept(route, changed, changed_headers, SECRET, queue)


def test_http_receiver_acknowledges_without_calling_provider_and_rejects_duplicate_headers(
    route, tmp_path
):
    with WebhookServer(("127.0.0.1", 0), settings(route), tmp_path, lambda _: SECRET) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            raw, headers = delivery()
            client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            client.request("POST", "/hooks/test", raw, headers)
            response = client.getresponse()
            assert response.status == 202 and json.loads(response.read())["state"] == "accepted"
            client.close()
            client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            client.putrequest("POST", "/hooks/test")
            client.putheader("Content-Length", str(len(raw)))
            client.putheader("Content-Length", str(len(raw)))
            client.endheaders(raw)
            assert client.getresponse().status == 400
            client.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert len(Queue(tmp_path).pending()) == 1


def test_worker_restart_retry_limit_and_policy_changes(route, tmp_path):
    queue = Queue(tmp_path)
    raw, headers = delivery()
    accept(route, raw, headers, SECRET, queue)
    job = queue.pending()[0]
    job.update(state="running", attempts=1)
    queue.save(job)  # Simulate death after acceptance and before a completed result.
    calls = []

    def process(current, hint):
        calls.append(hint["change"])
        return {"state": "done", "run_id": "saved-review"}

    resumed = work_once(settings(route), Queue(tmp_path), process)
    assert calls == [1] and resumed["state"] == "done" and resumed["attempts"] == 2
    assert work_once(settings(route), queue, process) is None
    raw, headers = delivery("synchronize", "next")
    accept(route, raw, headers, SECRET, queue)
    route.digest = "changed-policy"
    assert work_once(settings(route), queue, process)["state"] == "attention"
    assert calls == [1]


def test_worker_errors_do_not_busy_loop_or_retry_forever(route, tmp_path):
    queue = Queue(tmp_path)
    raw, headers = delivery()
    accept(route, raw, headers, SECRET, queue)

    def failing(*_):
        raise CirraError("provider temporarily unavailable")

    config = settings(route, max_attempts=2)
    job = work_once(config, queue, failing)
    assert job["state"] == "retry" and not queue.pending()
    job["not_before"] = 0
    queue.save(job)
    assert work_once(config, queue, failing)["state"] == "attention"
    assert not queue.pending()


def test_service_configuration_is_explicit_and_pins_policy(tmp_path, policy_data):
    policy_data["reviewer"] = {"kind": "claude", "endpoint": "cli://claude", "model": "test"}
    policy_data["repositories"] = [
        {"provider": "github", "instance": "https://github.com", "repository_id": "42"}
    ]
    (tmp_path / "policy.yaml").write_text(yaml.safe_dump(policy_data))
    data = {
        "version": 1,
        "routes": [
            {
                "name": "test",
                "provider": "github",
                "repository": "example/repo",
                "repository_id": "42",
                "policy": "policy.yaml",
                "webhook_secret_env": "HOOK_SECRET",
                "publisher": {"app_id": 5, "installation_id": 6, "private_key_file": "app.pem"},
            }
        ],
    }
    path = tmp_path / "service.yaml"
    path.write_text(yaml.safe_dump(data))
    route = load_service(path).routes["test"]
    assert route.policy.path == str(tmp_path / "policy.yaml")
    assert route.publisher["private_key_file"] == str(tmp_path / "app.pem")
    invalid = copy.deepcopy(data)
    invalid["routes"][0]["repository_id"] = "99"
    path.write_text(yaml.safe_dump(invalid))
    with pytest.raises(CirraError, match="allowlisted"):
        load_service(path)


def test_credential_file_loads_only_process_selected_names_without_shell_expansion(tmp_path):
    path = tmp_path / ".env"
    path.write_text("HOOK_SECRET='$(do-not-execute)'\nPUBLISH_TOKEN=not-for-this-process\n")
    selected = credential_file(path, {"HOOK_SECRET"})
    assert selected("HOOK_SECRET") == "$(do-not-execute)"
    assert selected(None) is None
    with pytest.raises(CirraError, match="not selected"):
        selected("PUBLISH_TOKEN")


def test_processor_reuses_review_and_reconciles_lost_publication(
    inputs, fixture_data, policy_data, tmp_path, monkeypatch
):
    from cirra.coordinator import review as real_review
    from cirra.worker import Processor

    fixture_data["change"].update(
        provider="github", instance="https://github.com", repository_id="42"
    )
    fixture_data["collection"] = {
        "source": "github_rest",
        "repository": "example/repo",
        "observed_at": "2026-10-15T00:00:00Z",
        "check_errors": [],
    }
    policy_data["repositories"] = [
        {"provider": "github", "instance": "https://github.com", "repository_id": "42"}
    ]
    provider, policy = inputs(data=fixture_data, policy=policy_data)
    route = SimpleNamespace(
        provider="github",
        repository="example/repo",
        repository_id="42",
        policy=policy,
        reader_env=None,
        observer=None,
        publisher={"app_id": 42},
    )
    calls = []

    class Reviewer:
        def review(self, packet):
            calls.append(packet)
            return '{"findings":[],"notes":[]}'

    class Publisher:
        principal_id = "42"
        items = []
        writes = 0

        def find(self, report, slot):
            return self.items

        def write(self, report, metadata, identifier):
            from cirra.transport import RemoteUncertain

            self.writes += 1
            self.items = [{"id": "100", "metadata": metadata}]
            raise RemoteUncertain("lost acknowledgement")

    publisher = Publisher()
    monkeypatch.setattr("cirra.worker.is_open", lambda *_: True)
    monkeypatch.setattr("cirra.worker.GitHubPublisher", lambda *_: publisher)
    monkeypatch.setattr(
        "cirra.worker.review", lambda p, c, s: real_review(p, c, s, reviewer=Reviewer())
    )
    processor = Processor(
        tmp_path / "service", lambda _: None, lambda *a, **kw: copy.deepcopy(fixture_data)
    )
    monkeypatch.setattr(processor, "app_token", lambda _: "token-not-sent-to-reviewer")
    first = processor(route, {"change": 1})
    second = processor(route, {"change": 1})
    assert first["state"] == "attention" and second["state"] == "done"
    assert first["run_id"] == second["run_id"] and len(calls) == publisher.writes == 1
    assert "token-not-sent-to-reviewer" not in canonical(calls)


def test_daily_admission_limit_survives_restart_without_losing_queued_work(route, tmp_path):
    queue = Queue(tmp_path)
    for action, identity in (("opened", "one"), ("synchronize", "two")):
        raw, headers = delivery(action, identity)
        accept(route, raw, headers, SECRET, queue)
    config = replace(settings(route), max_jobs_per_day=1)
    assert work_once(config, queue, lambda *_: {"state": "done"})["state"] == "done"
    assert (
        work_once(config, Queue(tmp_path), lambda *_: pytest.fail("must not spend again")) is None
    )
    assert len(queue.pending()) == 1 and queue.usage()["attempts"] == 1
