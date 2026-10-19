import copy
import hashlib
import hmac
import json
import socket
import threading
import time
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
from cirra.worker import Processor, work_once

SECRET = "independent-webhook-secret-32-characters"


def test_worker_selects_injected_publisher_key_with_explicit_env_file(tmp_path, monkeypatch):
    from cirra import service_cli

    pem = "synthetic-multiline\nprivate-key\n"
    monkeypatch.setenv("APP_PEM", pem)
    env_file = tmp_path / "worker.env"
    env_file.write_text("UNRELATED=not-selected\n")
    route = SimpleNamespace(
        reader_env="READER", witness_env="WITNESS", publisher={"private_key_env": "APP_PEM"}
    )
    config = SimpleNamespace(routes={"github": route})
    monkeypatch.setattr(service_cli, "load_service", lambda _: config)

    def worker(_config, _state, processor, **_kwargs):
        assert processor.credential("APP_PEM") == pem
        with pytest.raises(CirraError, match="not selected"):
            processor.credential("UNRELATED")
        return None

    monkeypatch.setattr(service_cli, "run_worker", worker)
    args = SimpleNamespace(
        command="worker",
        service_config=tmp_path / "service.yaml",
        env_file=env_file,
        state_dir=tmp_path,
        once=True,
    )
    assert service_cli.run(args, lambda _: None, None) == 0


def test_publisher_reads_explicit_private_key_environment_without_opening_file(
    tmp_path, monkeypatch
):
    route = SimpleNamespace(
        publisher={"app_id": 5, "installation_id": 6, "private_key_env": "APP_PEM"},
        repository_id="42",
    )
    calls = []
    monkeypatch.setattr(
        "cirra.worker.github_app_token",
        lambda *args: calls.append(args) or "synthetic-installation-token",
    )
    processor = Processor(
        tmp_path, lambda name: "synthetic-private-key" if name == "APP_PEM" else None, None
    )
    assert processor.app_token(route) == "synthetic-installation-token"
    assert calls == [(5, 6, "synthetic-private-key", "42")]
    processor.credential = lambda _: None
    with pytest.raises(CirraError, match="environment variable is missing"):
        processor.app_token(route)


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


def delivery(action="opened", identity="delivery-1", author="OWNER", **extra):
    pull_request = {"number": 1, "head": {"sha": "do-not-trust"}}
    if author is not None:
        pull_request["author_association"] = author
    raw = canonical(
        {
            "action": action,
            "repository": {"id": 42, "full_name": "example/repo"},
            "pull_request": pull_request,
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


@pytest.mark.parametrize(
    "author", ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "MANNEQUIN", None]
)
def test_untrusted_pull_request_authors_never_reach_the_queue(route, tmp_path, author):
    # Any GitHub account can open a pull request against a public repository.
    # Execution and review must not be spent on authors without write access.
    queue = Queue(tmp_path)
    raw, headers = delivery(author=author)
    assert accept(route, raw, headers, SECRET, queue) == {
        "state": "ignored",
        "reason": "untrusted author",
    }
    assert not queue.pending()


@pytest.mark.parametrize("author", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_trusted_pull_request_authors_are_queued(route, tmp_path, author):
    raw, headers = delivery(author=author)
    assert accept(route, raw, headers, SECRET, Queue(tmp_path))["state"] == "accepted"


def test_metadata_only_edits_do_not_spend_admission(route, tmp_path):
    queue = Queue(tmp_path)
    raw, headers = delivery("edited", changes={"title": {"from": "old title"}})
    assert accept(route, raw, headers, SECRET, queue) == {
        "state": "ignored",
        "reason": "metadata-only edit",
    }
    assert not queue.pending()
    raw, headers = delivery(
        "edited", identity="delivery-2", changes={"base": {"ref": {"from": "old-base"}}}
    )
    assert accept(route, raw, headers, SECRET, queue)["state"] == "accepted"


def gitlab_delivery(identity, **attributes):
    raw = canonical(
        {
            "project": {"id": 42, "path_with_namespace": "example/repo"},
            "object_attributes": {
                "iid": 1,
                "action": "update",
                "source_project_id": 42,
                "target_project_id": 42,
                **attributes,
            },
            "changes": {},
        }
    ).encode()
    return raw, {
        "X-Gitlab-Event": "Merge Request Hook",
        "X-Gitlab-Event-UUID": identity,
        "X-Gitlab-Token": SECRET,
    }


def test_gitlab_fork_and_metadata_only_merge_requests_are_ignored(route, tmp_path):
    gitlab = SimpleNamespace(
        **{
            **vars(route),
            "provider": "gitlab",
            "instance": "https://gitlab.com",
            "policy": replace(
                route.policy,
                repositories=(
                    {"provider": "gitlab", "instance": "https://gitlab.com", "repository_id": "42"},
                ),
            ),
        }
    )
    queue = Queue(tmp_path)
    raw, headers = gitlab_delivery("g1", action="open", source_project_id=99)
    assert accept(gitlab, raw, headers, SECRET, queue)["reason"] == "untrusted author"
    raw, headers = gitlab_delivery("g2", title="new title")
    assert accept(gitlab, raw, headers, SECRET, queue)["reason"] == "metadata-only edit"
    assert not queue.pending()
    raw, headers = gitlab_delivery("g3", oldrev="0" * 40)
    assert accept(gitlab, raw, headers, SECRET, queue)["state"] == "accepted"


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


def test_non_ascii_signature_is_an_authentication_failure_not_a_crash(route, tmp_path):
    # Production audit X05: hmac.compare_digest raised TypeError on a non-ASCII
    # header, which handle_error swallowed without a response record.
    raw, headers = delivery()
    headers["X-Hub-Signature-256"] = "sha256=" + "é" * 64
    with pytest.raises(CirraError, match="authentication"):
        accept(route, raw, headers, SECRET, Queue(tmp_path))


def serve(route, tmp_path, **attributes):
    server = WebhookServer(("127.0.0.1", 0), settings(route), tmp_path, lambda _: SECRET)
    for name, value in attributes.items():
        setattr(server, name, value)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_receiver_closes_slow_requests_at_a_total_deadline(route, tmp_path):
    # Production audit C011/C075: a 5 s per-read timeout let a client trickling
    # bytes hold one of 16 slots for hours.
    server, thread = serve(route, tmp_path, request_deadline=0.5)
    try:
        slow = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        slow.sendall(b"POST /hooks/test HTTP/1.1\r\nContent-Length: 100\r\n")
        started = time.monotonic()
        closed = False
        while time.monotonic() - started < 4:
            try:
                slow.sendall(b"X-Slow: 1\r\n")
                if slow.recv(1024, socket.MSG_PEEK) == b"":
                    closed = True
                    break
            except (ConnectionError, OSError):
                closed = True
                break
            time.sleep(0.2)
        slow.close()
        assert closed and time.monotonic() - started < 3
        raw, headers = delivery()
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        client.request("POST", "/hooks/test", raw, headers)
        assert client.getresponse().status == 202
        client.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_receiver_logs_one_line_per_request_without_headers_or_body(route, tmp_path, capsys):
    # Production audit C059/C066: refused deliveries left no local trace.
    server, thread = serve(route, tmp_path)
    try:
        raw, headers = delivery(identity="delivery-logged")
        headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        client.request("POST", "/hooks/test", raw, headers)
        assert client.getresponse().status == 401
        client.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert {
        "event": "webhook_request",
        "method": "POST",
        "path": "/hooks/test",
        "status": 401,
        "delivery": "delivery-logged",
        "bytes": len(raw),
    } in lines
    assert SECRET not in json.dumps(lines) and "sha256" not in json.dumps(lines)


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


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_service_configuration_is_explicit_and_pins_policy(tmp_path, policy_data, provider):
    policy_data["reviewer"] = {"kind": "claude", "endpoint": "cli://claude", "model": "test"}
    policy_data["repositories"] = [
        {"provider": provider, "instance": f"https://{provider}.com", "repository_id": "42"}
    ]
    (tmp_path / "policy.yaml").write_text(yaml.safe_dump(policy_data))
    data = {
        "version": 1,
        "routes": [
            {
                "name": "test",
                "provider": provider,
                "repository": "example/repo",
                "repository_id": "42",
                "policy": "policy.yaml",
                "webhook_secret_env": "HOOK_SECRET",
                "publisher": {"app_id": 5, "installation_id": 6, "private_key_file": "app.pem"},
                "observer": "observer.yaml",
                "witness_key_env": "WITNESS_KEY",
            }
        ],
    }
    if provider == "gitlab":
        data["routes"][0]["publisher"] = {
            "principal_id": 7,
            "token_env": "PUBLISHER_TOKEN",
            "permission_receipt": "permissions.json",
        }
    path = tmp_path / "service.yaml"
    path.write_text(yaml.safe_dump(data))
    route = load_service(path).routes["test"]
    assert route.policy.path == str(tmp_path / "policy.yaml")
    if provider == "github":
        assert route.publisher["private_key_file"] == str(tmp_path / "app.pem")
        env_config = copy.deepcopy(data)
        env_config["routes"][0]["publisher"].pop("private_key_file")
        env_config["routes"][0]["publisher"]["private_key_env"] = "APP_PEM"
        path.write_text(yaml.safe_dump(env_config))
        assert load_service(path).routes["test"].publisher["private_key_env"] == "APP_PEM"
        env_config["routes"][0]["publisher"]["private_key_file"] = "app.pem"
        path.write_text(yaml.safe_dump(env_config))
        with pytest.raises(CirraError, match="exactly one"):
            load_service(path)
    else:
        assert route.publisher["permission_receipt"] == str(tmp_path / "permissions.json")
    assert route.observer == tmp_path / "observer.yaml"
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


def test_gitlab_worker_observes_reviews_and_updates_one_note_with_separate_credentials(
    inputs, fixture_data, policy_data, tmp_path, monkeypatch
):
    from cirra import provenance
    from cirra.coordinator import review as real_review
    from cirra.observer import load_observer
    from cirra.store import Store
    from cirra.worker import Processor

    key, reader, publisher_token = "independent-witness-key-32-characters", "reader", "publisher"
    observer_file = tmp_path / "observer.yaml"
    observer_file.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "image": "sha256:" + "1" * 64,
                "commands": [["python", "test.py"]],
            }
        )
    )
    definition = load_observer(observer_file)["definition_digest"]
    fixture_data["change"].update(
        provider="gitlab", instance="https://gitlab.com", repository_id="42"
    )
    fixture_data["collection"] = {
        "source": "gitlab_rest",
        "repository": "example/repo",
        "observed_at": "2026-10-16T00:00:00Z",
        "check_errors": [],
    }
    fixture_data["checks"] = []
    policy_data["repositories"] = [
        {
            "provider": "gitlab",
            "instance": "https://gitlab.com",
            "repository_id": "42",
        }
    ]
    policy_data["ci"]["expected"] = [
        {
            "kind": "observer_run",
            "source_id": "cirra/docker",
            "name": "cirra/isolated-tests",
            "definition_digest": definition,
        }
    ]
    _, policy = inputs(data=fixture_data, policy=policy_data)
    permission = tmp_path / "publisher-permissions.json"
    permission.write_text(
        json.dumps(
            {
                "version": 1,
                "instance": "https://gitlab.com",
                "repository_id": "42",
                "principal_id": "7",
                "token_sha256": hashlib.sha256(publisher_token.encode()).hexdigest(),
                "checked_at": "2026-10-16T00:00:00Z",
                "evidence": "Operator test scope receipt",
                "note_read_create_update": True,
                "no_merge_or_repository_write": True,
            }
        )
    )
    route = SimpleNamespace(
        provider="gitlab",
        name="gitlab",
        repository="example/repo",
        repository_id="42",
        policy=policy,
        reader_env="READER",
        observer=observer_file,
        witness_env="WITNESS",
        publisher={
            "principal_id": 7,
            "token_env": "PUBLISHER",
            "permission_receipt": str(permission),
        },
    )
    executions, calls, writes = [], [], []

    def source(repository, change, head, directory, token, *, provider):
        assert token == reader and provider == "gitlab"
        directory.mkdir()
        path = directory / "source.tar"
        path.write_bytes(b"verified source")
        return path, []

    class Executor:
        def run(self, policy, archive, directory, name):
            executions.append(name)
            return {"container_id": "a" * 64, "exit_code": 0}

    class Reviewer:
        def review(self, packet):
            calls.append(packet)
            return '{"findings":[],"notes":[]}'

    class Publisher:
        principal_id = "7"
        items = []

        def __init__(self, client, principal):
            assert client.http.headers["PRIVATE-TOKEN"] == publisher_token and principal == 7

        def find(self, report, slot):
            return self.items

        def write(self, report, metadata, identifier):
            assert report["status"] == "complete" and report["ci"]["outcome"] == "verified"
            writes.append(identifier)
            self.items[:] = [{"id": "100", "metadata": metadata}]
            return self.items[0]

    def capture(provider, repository, change, config, *, token, witness=None, witness_key=None):
        assert provider == "gitlab" and token == reader
        data = copy.deepcopy(fixture_data)
        return provenance.apply(data, witness, witness_key) if witness else data

    monkeypatch.setattr("cirra.worker.is_open", lambda *_: True)
    monkeypatch.setattr("cirra.worker.GitLabPublisher", Publisher)
    monkeypatch.setattr("cirra.observer.export_source", source)
    monkeypatch.setattr("cirra.observer.DockerExecutor", Executor)
    monkeypatch.setattr(
        "cirra.worker.review", lambda p, c, s: real_review(p, c, s, reviewer=Reviewer())
    )
    credential = {"READER": reader, "PUBLISHER": publisher_token, "WITNESS": key}.__getitem__
    processor = Processor(tmp_path / "service", credential, capture)
    monkeypatch.setattr(
        processor, "app_token", lambda _: pytest.fail("GitLab must not mint an App token")
    )
    first = processor(route, {"change": 1})
    second = processor(route, {"change": 1})
    assert first["state"] == second["state"] == "done"
    assert first["run_id"] == second["run_id"]
    assert len(calls) == len(executions) == 1 and writes == [None, "100"]
    assert publisher_token not in canonical(calls) and key not in canonical(calls)
    report = Store(tmp_path / "service").latest_report(first["run_id"])
    assert report["ci"]["expected"][0]["evidence"]["kind"] == "observer_run"
