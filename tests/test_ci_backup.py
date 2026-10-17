import copy
import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from cirra.domain import CirraError
from cirra.job_queue import Queue
from cirra.publish import write_receipt
from cirra.service_config import load_service
from cirra.worker import run_worker, work_once


def test_manual_request_and_requeue_keep_history_and_daily_budget(tmp_path):
    route = SimpleNamespace(
        name="repo",
        digest="v1",
        provider="github",
        instance="https://github.com",
        repository="owner/repo",
        repository_id="42",
    )
    queue = Queue(tmp_path)
    requested = queue.request(route, 7)
    job = queue.pending()[0]
    assert job["hint"]["change"] == 7
    assert job["hint"]["source"] == "manual"
    queue.reserve_attempt(30)
    job.update(state="attention", attempts=3, error="provider unavailable")
    queue.save(job)
    recovered = queue.requeue(requested["job_id"], route, "provider recovered")
    assert recovered["attempts"] == 0 and recovered["state"] == "queued"
    assert queue.usage()["attempts"] == 1
    history = list((queue.directory / "history").glob("*.json"))
    assert len(history) == 1
    assert json.loads(history[0].read_text())["previous"]["error"] == "provider unavailable"
    with pytest.raises(CirraError, match="terminal"):
        queue.requeue(job["id"], route, "do not reset active work")
    with pytest.raises(CirraError):
        queue.requeue("../outside", route, "invalid")


def test_ci_only_route_requires_observer_but_not_automatic_reviewer(tmp_path, policy_data):
    policy_data["repositories"] = [
        {"provider": "github", "instance": "https://github.com", "repository_id": "42"}
    ]
    (tmp_path / "policy.yaml").write_text(yaml.safe_dump(policy_data))
    route = {
        "name": "ci",
        "provider": "github",
        "repository": "owner/repo",
        "repository_id": "42",
        "policy": "policy.yaml",
        "webhook_secret_env": "HOOK",
        "publisher": {"app_id": 1, "installation_id": 2, "private_key_file": "app.pem"},
        "review_enabled": False,
        "observer": "observer.yaml",
        "witness_key_env": "KEY",
    }
    path = tmp_path / "service.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "routes": [route]}))
    assert load_service(path).routes["ci"].review_enabled is False
    route.pop("observer")
    route.pop("witness_key_env")
    path.write_text(yaml.safe_dump({"version": 1, "routes": [route]}))
    with pytest.raises(CirraError, match="observer"):
        load_service(path)


def test_outage_retries_back_off_without_losing_completed_ci(tmp_path):
    route = SimpleNamespace(name="ci", digest="v1")
    queue = Queue(tmp_path)
    queue.enqueue(route, {"delivery": "one", "payload_digest": "one"})
    config = SimpleNamespace(
        routes={"ci": route},
        max_attempts=10,
        max_jobs_per_day=30,
        retry_seconds=30,
        max_retry_seconds=3600,
    )

    def failing(*_):
        raise CirraError("provider unavailable")

    first = work_once(config, queue, failing)
    first.update(not_before=0)
    queue.save(first)
    import time

    before = time.time()
    second = work_once(config, queue, failing)
    assert second["state"] == "retry"
    assert second["not_before"] >= before + 60


@pytest.mark.parametrize("review_enabled", [False, True])
def test_processor_preserves_ci_with_disabled_or_failed_reviewer(
    inputs, fixture_data, policy_data, tmp_path, monkeypatch, review_enabled
):
    from cirra.worker import Processor

    data = copy.deepcopy(fixture_data)
    data["change"].update(provider="github", instance="https://github.com", repository_id="42")
    data["collection"] = {
        "source": "github_rest",
        "repository": "owner/repo",
        "observed_at": "2026-10-16T00:00:00Z",
        "check_errors": [],
    }
    _, policy = inputs()
    policy = replace(
        policy,
        repositories=(
            {"provider": "github", "instance": "https://github.com", "repository_id": "42"},
        ),
        expected_checks=(
            {
                "kind": "check_run",
                "source_id": "42",
                "name": "cirra/isolated-tests",
                "definition_digest": "a" * 64,
            },
        ),
    )
    route = SimpleNamespace(
        name="ci",
        provider="github",
        repository="owner/repo",
        repository_id="42",
        policy=policy,
        reader_env=None,
        observer=tmp_path / "observer.yaml",
        witness_env="KEY",
        publisher={"app_id": 42},
        review_enabled=review_enabled,
    )
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"statement": {"checks": [{"check": {"raw_state": "failure"}}]}}))
    monkeypatch.setattr("cirra.worker.is_open", lambda *_: True)
    monkeypatch.setattr("cirra.worker.load_observer", lambda _: {"definition_digest": "a" * 64})

    def observation(*args, **kwargs):
        kwargs["on_execution"](receipt)
        return receipt

    monkeypatch.setattr("cirra.worker.observe", observation)

    def failed_review(*_):
        if not review_enabled:
            pytest.fail("CI-only called model")
        return {"run_id": "unavailable-review", "review": {"outcome": "uncertain"}}, False

    monkeypatch.setattr("cirra.worker.review", failed_review)
    monkeypatch.setattr("cirra.worker.export_report", lambda *_: None)
    processor = Processor(tmp_path / "state", lambda _: "key", lambda *a, **kw: data)
    result = processor(route, {"change": 7})
    assert result["state"] == ("attention" if review_enabled else "done")
    if not review_enabled:
        assert result["review"] == "disabled"
    assert result["ci"]["conclusion"] == "failure"
    assert len(list((tmp_path / "state" / "ci").glob("ci-*.json"))) == 1


def test_reconciliation_recovers_missed_revisions_without_repeated_jobs(tmp_path):
    from cirra.reconcile import sync

    route = SimpleNamespace(
        name="repo",
        digest="v1",
        provider="github",
        instance="https://github.com",
        repository="owner/repo",
        repository_id="42",
        reader_env=None,
    )
    queue = Queue(tmp_path)
    config = SimpleNamespace(routes={"repo": route})
    revision = {"head": "a" * 40, "target": "b" * 40}

    def discover(*_):
        return [(7, revision.copy())]

    first = sync(config, queue, lambda _: None, discover_changes=discover)
    second = sync(config, queue, lambda _: None, discover_changes=discover)
    assert not first[0]["jobs"][0]["duplicate"]
    assert second[0]["jobs"][0]["duplicate"]
    revision["target"] = "c" * 40
    assert not sync(config, queue, lambda _: None, discover_changes=discover)[0]["jobs"][0][
        "duplicate"
    ]
    assert len(queue.pending()) == 2


def test_discovery_verifies_repository_and_reads_current_target(tmp_path, monkeypatch):
    from cirra.reconcile import discover

    route = SimpleNamespace(provider="github", repository="owner/repo", repository_id="42")

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, path="", params=None):
            return {
                "": {"id": 42},
                "pulls": [
                    {
                        "id": 100,
                        "number": 7,
                        "draft": False,
                        "author_association": "OWNER",
                        "head": {"sha": "a" * 40},
                        "base": {"repo": {"id": 42}, "ref": "main", "sha": "b" * 40},
                    }
                ],
                "branches/main": {"name": "main", "commit": {"sha": "c" * 40}},
            }[path], False

    monkeypatch.setattr("cirra.reconcile.GitHubClient", Client)
    assert discover(route) == [(7, {"head": "a" * 40, "target": "c" * 40})]
    route.repository_id = "99"
    with pytest.raises(CirraError, match="identity"):
        discover(route)


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_discovery_skips_changes_from_authors_without_write_access(monkeypatch, provider):
    # Polling must apply the same author trust rule as the webhook receiver,
    # or a fork PR would still spend execution and review through reconcile.
    from cirra.reconcile import discover

    route = SimpleNamespace(provider=provider, repository="owner/repo", repository_id="42")
    if provider == "github":
        items = [
            {
                "id": 100 + number,
                "number": number,
                "draft": False,
                "author_association": association,
                "head": {"sha": head * 40},
                "base": {"repo": {"id": 42}, "ref": "main"},
            }
            for number, association, head in ((7, "MEMBER", "a"), (8, "CONTRIBUTOR", "d"))
        ]
        responses = {"pulls": items, "branches/main": {"name": "main", "commit": {"sha": "c" * 40}}}
    else:
        items = [
            {
                "id": 100 + iid,
                "iid": iid,
                "project_id": 42,
                "source_project_id": source,
                "target_project_id": 42,
                "sha": head * 40,
                "target_branch": "main",
            }
            for iid, source, head in ((7, 42, "a"), (8, 99, "d"))
        ]
        responses = {
            "merge_requests": items,
            "repository/branches/main": {"name": "main", "commit": {"id": "c" * 40}},
        }
    responses[""] = {"id": 42}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, path="", params=None):
            return responses[path], False

    monkeypatch.setattr(
        f"cirra.reconcile.{'GitHub' if provider == 'github' else 'GitLab'}Client", Client
    )
    assert discover(route) == [(7, {"head": "a" * 40, "target": "c" * 40})]


def test_branch_requests_are_allowlisted_and_deduplicated(tmp_path):
    route = SimpleNamespace(
        name="repo",
        digest="v1",
        provider="github",
        instance="https://github.com",
        repository="owner/repo",
        repository_id="42",
        branches=("main",),
    )
    queue = Queue(tmp_path)
    first = queue.request_branch(route, "main", revision="a" * 40)
    assert queue.request_branch(route, "main", revision="a" * 40)["duplicate"]
    assert not queue.request_branch(route, "main", revision="b" * 40)["duplicate"]
    assert queue.pending()[0]["hint"]["branch"] == "main"
    assert first["job_id"]
    with pytest.raises(CirraError, match="allowlisted"):
        queue.request_branch(route, "untrusted")


@pytest.mark.parametrize(
    "branch", ["../main", "main:other", "--upload-pack=evil", "a.lock", "a//b"]
)
def test_branch_policy_refuses_refspecs_and_unsafe_names(branch):
    from cirra.service_config import branch_name

    with pytest.raises(CirraError):
        branch_name(branch)


class StopAfter(threading.Event):
    """Idle-loop stand-in: each poll wait returns at once; stop after `passes` waits."""

    def __init__(self, passes):
        super().__init__()
        self.passes = passes

    def wait(self, timeout=None):
        self.passes -= 1
        if self.passes <= 0:
            self.set()
        return self.is_set()


def test_idle_worker_heartbeat_overwrites_status_without_growing_history(tmp_path, monkeypatch):
    # Production audit X06: every idle pass wrote a new fsync'd history file
    # (about 43,200 a day at the default 2-second poll).
    ticks = iter(f"2026-10-17T00:00:{second:02d}Z" for second in range(60))
    monkeypatch.setattr("cirra.worker.utc_now", lambda: next(ticks))
    config = SimpleNamespace(
        routes={}, max_jobs=10, poll_seconds=2, reconcile_seconds=0, max_attempts=3
    )
    run_worker(config, tmp_path, process=None, stopped=StopAfter(5))
    status = json.loads((tmp_path / "worker-status.json").read_text())
    assert status["state"] == "running"
    assert status["heartbeat_at"] == "2026-10-17T00:00:04Z"
    assert not (tmp_path / "history" / "worker-status").exists()


def test_write_receipt_keeps_history_unless_the_caller_opts_out(tmp_path):
    write_receipt(tmp_path / "job.json", {"attempt": 1})
    write_receipt(tmp_path / "job.json", {"attempt": 2})
    assert len(list((tmp_path / "history" / "job").iterdir())) == 2
    write_receipt(tmp_path / "status.json", {"tick": 1}, history=False)
    write_receipt(tmp_path / "status.json", {"tick": 2}, history=False)
    assert json.loads((tmp_path / "status.json").read_text()) == {"tick": 2}
    assert not (tmp_path / "history" / "status").exists()
