"""Service availability is separate from audit completion and recovery readiness."""

import json
import threading
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from types import SimpleNamespace

import pytest
from audit_fixtures import audit_policy

from cirra import audits
from cirra.health import check, main
from cirra.job_queue import AuditQueue, Queue
from cirra.locking import change_lock
from cirra.publish import write_receipt
from cirra.store import Store
from cirra.webhook import WebhookServer
from cirra.worker import heartbeat


@pytest.fixture
def receiver(tmp_path):
    config = SimpleNamespace(routes={}, max_jobs=1)
    with WebhookServer(("127.0.0.1", 0), config, tmp_path, lambda _: None) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)


def get(server):
    client = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        client.request("GET", "/healthz")
        response = client.getresponse()
        return response.status, json.loads(response.read())
    finally:
        client.close()


def test_standalone_health_refuses_capacity_and_recovers_without_admitting_work(receiver):
    assert get(receiver)[0] == 200
    receiver.queue.slots.record(1)
    assert get(receiver) == (503, {"status": "admission unavailable"})
    receiver.queue.slots.record(0)
    assert get(receiver)[0] == 200
    assert receiver.queue.scan() == ([], [])


def test_standalone_health_refuses_an_unwritable_queue(receiver, monkeypatch):
    import tempfile

    def denied(*args, **kwargs):
        raise PermissionError("private-fixture-path")

    monkeypatch.setattr(tempfile, "TemporaryFile", denied)
    assert get(receiver) == (503, {"status": "admission unavailable"})


def test_standalone_health_refuses_invalid_capacity_record(receiver):
    receiver.queue.slots.path.write_text("{}")
    assert get(receiver) == (503, {"status": "admission unavailable"})


def test_standalone_health_does_not_wait_for_a_busy_admission_lock(receiver):
    with change_lock(receiver.queue.directory / "locks", "enqueue", 0):
        assert get(receiver) == (503, {"status": "admission unavailable"})
    assert get(receiver)[0] == 200


def test_standalone_health_refuses_pending_legacy_census(receiver):
    for number in range(34):
        (receiver.queue.directory / f"{number}.json").write_text("{}")
    assert get(receiver) == (503, {"status": "admission unavailable"})


def test_coordinated_health_keeps_using_shared_admission(receiver, monkeypatch):
    # A shared receiver admits to quorum, independently of local review slots.
    receiver.queue.slots.record(1)
    calls = []
    shared = SimpleNamespace(check=lambda: calls.append("checked"))
    receiver.shared_inbox = shared
    monkeypatch.setattr(receiver, "intake", lambda: shared)
    assert get(receiver)[0] == 200 and calls == ["checked"]


@pytest.fixture
def audit_world(tmp_path, policy_data):
    policy = audit_policy(tmp_path, policy_data)
    store = Store(tmp_path)
    queue = AuditQueue(tmp_path, store)
    identity = audits.audit_identity(policy.audit, "full", policy.repositories[0], "a" * 40)
    heartbeat(Queue(tmp_path))
    return tmp_path, policy, store, queue, identity


@pytest.mark.parametrize("state", ["awaiting_import", "deferred"])
def test_stale_session_audits_warn_without_failing_availability(audit_world, state, capsys):
    directory, _, _, queue, identity = audit_world
    job_id = queue.request(identity, "repository", reason="schedule")["job_id"]
    job = queue.read(job_id)
    job.update(state=state, accepted_at=(datetime.now(UTC) - timedelta(days=3)).isoformat())
    queue.save(job)  # A fresh poll timestamp must not hide an old request.
    result = check(directory, include_metrics=True)
    assert result["healthy"] and result["problems"] == []
    assert any(w["kind"] == "audit-backlog" and w["state"] == state for w in result["warnings"])
    assert result["audit_metrics"]["waiting"][state]["count"] == 1
    assert main(directory, 1200, None) == 0
    assert json.loads(capsys.readouterr().out)["warnings"]


def test_baseline_uses_configured_age_and_repository_scope(audit_world):
    directory, _, store, _, identity = audit_world
    baseline, _ = audits.begin(store, identity)
    structural, _ = audits.begin(store, {**identity, "head": "b" * 40}, "change:7")
    now = datetime.now(UTC)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE audits SET state='complete', completed_at=? WHERE attempt_id=?",
            ((now - timedelta(days=4)).timestamp(), baseline["attempt_id"]),
        )
        connection.execute(
            "UPDATE audits SET state='complete', completed_at=? WHERE attempt_id=?",
            (now.timestamp(), structural["attempt_id"]),
        )
    key = audits.repository_key(identity["repository"])
    result = check(directory, now=now, audit_policies={key: 3}, include_metrics=True)
    assert result["healthy"]
    assert any(w["kind"] == "audit-baseline" for w in result["warnings"])
    [row] = result["audit_metrics"]["baselines"]
    assert row["attempt_id"] == baseline["attempt_id"] and row["overdue"]
    assert row["age_seconds"] == 4 * 86400
    fresh = check(directory, now=now, audit_policies={key: 5}, include_metrics=True)
    assert fresh["warnings"] == []
    assert not fresh["audit_metrics"]["baselines"][0]["overdue"]


def test_health_does_not_create_a_database_for_baseline_visibility(tmp_path):
    heartbeat(Queue(tmp_path))
    result = check(tmp_path, audit_policies={"a" * 64: 14})
    assert result["healthy"]
    assert any(w["kind"] == "audit-baseline" for w in result["warnings"])
    assert not (tmp_path / "cirra.sqlite3").exists()


def test_baseline_age_without_policy_does_not_invent_an_expiry(audit_world):
    directory, _, store, _, identity = audit_world
    baseline, _ = audits.begin(store, identity)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE audits SET state='complete', completed_at=? WHERE attempt_id=?",
            ((datetime.now(UTC) - timedelta(days=30)).timestamp(), baseline["attempt_id"]),
        )
    result = check(directory, include_metrics=True)
    assert result["healthy"] and result["warnings"] == []
    assert result["audit_metrics"]["baseline_policy_known"] is False
    assert result["audit_metrics"]["baselines"][0]["overdue"] is None


def test_health_main_reads_age_policy_from_service_routes(audit_world, capsys):
    directory, policy, _, _, identity = audit_world
    route = SimpleNamespace(policy=policy, **identity["repository"])
    service = SimpleNamespace(routes={"fixture": route})
    assert main(directory, 1200, None, service) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["audit_metrics"]["baseline_policy_known"]
    assert result["audit_metrics"]["baselines"][0]["max_age_days"] == policy.audit.max_age_days
    assert result["warnings"][0]["kind"] == "audit-baseline"


@pytest.mark.parametrize("age", [None, 22 * 3600, 23 * 3600])
def test_standby_restore_freshness_uses_matching_authority(audit_world, age):
    directory, _, _, _, _ = audit_world
    now = datetime.now(UTC)
    heartbeat(Queue(directory), state="standby")
    authority = {
        "host": "1" * 64,
        "epoch": "2" * 64,
        "tip": "3" * 64,
        "session": "4" * 64,
        "floor": int(now.timestamp()) - 86400,
        "target": None,
    }
    write_receipt(directory / "coordination-state.json", {"cluster": "fixture"})
    write_receipt(directory / "data-authority.json", authority)
    if age is not None:
        write_receipt(
            directory / "data-restored.json",
            {
                **{k: authority[k] for k in ("host", "epoch", "tip")},
                "snapshot_id": "5" * 64,
                "captured_at": (now - timedelta(seconds=age)).isoformat(),
            },
        )
    result = check(directory, now=now, include_metrics=True)
    assert any(p["kind"] == "restore" for p in result["problems"]) == (
        age is None or age >= 23 * 3600
    )
    assert not any(p["kind"] == "backup" for p in result["problems"])
    assert result["recovery_metrics"]["restore"]["age_seconds"] == age
    if age is not None and age < 23 * 3600:
        restored_path = directory / "data-restored.json"
        restored = json.loads(restored_path.read_text())
        naive = now.replace(tzinfo=None).isoformat()
        write_receipt(restored_path, {**restored, "captured_at": naive})
        assert any(p["kind"] == "restore" for p in check(directory)["problems"])
        write_receipt(restored_path, restored)
        authority["tip"] = "6" * 64
        write_receipt(directory / "data-authority.json", authority)
        assert any(p["kind"] == "restore" for p in check(directory)["problems"])
