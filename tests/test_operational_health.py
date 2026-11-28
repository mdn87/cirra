"""Service availability is separate from audit completion and recovery readiness."""

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest
from audit_fixtures import audit_policy

from cirra import audits, health
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


@pytest.fixture
def recovery_cache(tmp_path):
    now = datetime.now(UTC)
    authority = {
        "host": "1" * 64,
        "epoch": "2" * 64,
        "tip": "3" * 64,
        "session": "4" * 64,
        "floor": int(now.timestamp()) - 3 * 86400,
        "target": None,
    }
    heartbeat(Queue(tmp_path), state="standby")
    write_receipt(tmp_path / "coordination-state.json", {"cluster": "fixture"})
    write_receipt(tmp_path / "data-authority.json", authority)
    installed = {
        **{key: authority[key] for key in ("host", "epoch", "tip")},
        "snapshot_id": "5" * 64,
        "captured_at": (now - timedelta(days=2)).isoformat(),
    }
    write_receipt(tmp_path / "data-restored.json", installed)
    archive = tmp_path / "private-cache.zip"
    payload = b"fixture bytes verified by the native fetch path"
    archive.write_bytes(payload)
    archive.chmod(0o600)
    receipt = {
        **installed,
        "status": "fetched",
        "restic_snapshot_id": "6" * 64,
        "tip": "7" * 64,
        "snapshot_id": "8" * 64,
        "captured_at": (now - timedelta(minutes=1)).isoformat(),
        "archive_digest": hashlib.sha256(payload).hexdigest(),
    }
    metadata = archive.with_name(archive.name + ".receipt.json")
    write_receipt(metadata, receipt, history=False)
    config = SimpleNamespace(routes={}, coordination=SimpleNamespace(recovery_archive=archive))
    return SimpleNamespace(
        root=tmp_path,
        now=now,
        authority=authority,
        installed=installed,
        archive=archive,
        receipt=receipt,
        metadata=metadata,
        config=config,
    )


def cache_health(world):
    return check(world.root, now=world.now, service_config=world.config, include_metrics=True)


def test_fresh_verified_cache_keeps_stale_installed_standby_ready(recovery_cache, capsys):
    world = recovery_cache
    before = (world.root / "data-restored.json").read_bytes()
    result = cache_health(world)
    assert result["healthy"] and result["problems"] == []
    recovery = result["recovery_metrics"]
    assert recovery["standby_source"] == "cached"
    assert recovery["restore"]["age_seconds"] == 2 * 86400
    assert not recovery["restore"]["eligible"]
    assert recovery["restore_cache"]["age_seconds"] == 60
    assert recovery["restore_cache"]["eligible"]
    assert recovery["restore_cache"]["status"] == "verified"
    # The public CLI also passes the loaded service configuration through.
    assert main(world.root, 1200, None, world.config) == 0
    assert json.loads(capsys.readouterr().out)["recovery_metrics"]["standby_source"] == "cached"
    assert (world.root / "data-restored.json").read_bytes() == before
    assert world.receipt["archive_digest"] not in json.dumps(result)
    assert str(world.archive) not in json.dumps(result)
    assert not check(world.root, now=world.now)["healthy"]  # No config stays conservative.


@pytest.mark.parametrize(
    "age,ready", [(-61, False), (-60, True), (22 * 3600, True), (23 * 3600, False)]
)
def test_cached_capture_freshness_uses_capture_not_receipt_mtime(recovery_cache, age, ready):
    world = recovery_cache
    world.receipt["captured_at"] = (world.now - timedelta(seconds=age)).isoformat()
    write_receipt(world.metadata, world.receipt, history=False)
    result = cache_health(world)
    assert result["healthy"] is ready
    assert result["recovery_metrics"]["restore_cache"]["age_seconds"] == age


@pytest.mark.parametrize(
    "key,value",
    [
        ("host", "a" * 64),
        ("epoch", "b" * 64),
        ("snapshot_id", "bad"),
        ("restic_snapshot_id", "bad"),
        ("tip", "bad"),
        ("archive_digest", "bad"),
        ("status", "unchanged"),
        ("captured_at", "2026-11-28T00:00:00"),
    ],
)
def test_unverified_or_wrong_tenure_cache_does_not_hide_stale_restore(recovery_cache, key, value):
    world = recovery_cache
    write_receipt(world.metadata, {**world.receipt, key: value}, history=False)
    result = cache_health(world)
    assert not result["healthy"]
    assert [p["kind"] for p in result["problems"]] == ["restore"]
    assert not result["recovery_metrics"]["restore_cache"]["eligible"]


def test_tampered_cache_is_rejected_and_fresh_installed_restore_is_a_fallback(recovery_cache):
    world = recovery_cache
    world.archive.write_bytes(b"tampered cache bytes")
    result = cache_health(world)
    assert not result["healthy"]
    assert result["recovery_metrics"]["restore_cache"]["status"] == "digest_mismatch"
    write_receipt(
        world.root / "data-restored.json",
        {
            **world.installed,
            "captured_at": (world.now - timedelta(hours=1)).isoformat(),
        },
    )
    result = cache_health(world)
    assert result["healthy"] and result["recovery_metrics"]["standby_source"] == "installed"
    assert not result["recovery_metrics"]["restore_cache"]["eligible"]


@pytest.mark.parametrize("target", ["archive", "metadata"])
def test_cache_refuses_symlink_evidence(recovery_cache, target):
    world = recovery_cache
    original = getattr(world, target)
    moved = original.with_name(original.name + ".original")
    original.rename(moved)
    try:
        original.symlink_to(moved)
    except OSError:
        pytest.skip("symlink creation unavailable")
    assert not cache_health(world)["healthy"]


@pytest.mark.parametrize("target", ["archive", "metadata"])
def test_cache_refuses_hardlinked_evidence(recovery_cache, target):
    world = recovery_cache
    original = getattr(world, target)
    original.with_name(original.name + ".alias").hardlink_to(original)
    assert not cache_health(world)["healthy"]


@pytest.mark.parametrize("target", ["archive", "metadata"])
@pytest.mark.parametrize("replacement", ["missing", "directory"])
def test_cache_requires_available_regular_evidence(recovery_cache, target, replacement):
    world = recovery_cache
    original = getattr(world, target)
    original.rename(original.with_name(original.name + ".saved"))
    if replacement == "directory":
        original.mkdir()
    assert not cache_health(world)["healthy"]


@pytest.mark.parametrize("target", ["archive", "metadata"])
@pytest.mark.parametrize("field,value", [("st_uid", 999), ("st_mode", 0o100640)])
def test_cache_refuses_foreign_owner_and_shared_permissions(
    recovery_cache, monkeypatch, target, field, value
):
    world = recovery_cache
    native_stat = Path.lstat

    def observed(path):
        info = native_stat(path)
        values = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
        values.update(st_uid=123, st_mode=0o100600)
        if path == getattr(world, target):
            values[field] = value
        return SimpleNamespace(**values)

    monkeypatch.setattr(Path, "lstat", observed)
    monkeypatch.setattr(
        health,
        "os",
        SimpleNamespace(name="posix", geteuid=lambda: 123, fstat=os.fstat, path=os.path),
    )
    assert not cache_health(world)["healthy"]


@pytest.mark.parametrize("target", ["archive", "metadata"])
def test_cache_rejects_replacement_during_validation(recovery_cache, monkeypatch, target):
    world = recovery_cache
    original_reader = health.read_locked_file

    @contextmanager
    def replaced(path):
        with original_reader(path) as stream:
            if path == getattr(world, target):
                replacement = path.with_name(path.name + ".next")
                replacement.write_bytes(path.read_bytes())
                replacement.chmod(0o600)
                # Keep the original handle open, as a native refresh can do.
                os.replace(replacement, path)
            yield stream

    monkeypatch.setattr(health, "read_locked_file", replaced)
    assert not cache_health(world)["healthy"]


@pytest.mark.parametrize("limit", ["RECOVERY_CACHE_MAX_BYTES", "RECOVERY_RECEIPT_MAX_BYTES"])
def test_cache_validation_refuses_oversize_evidence(recovery_cache, monkeypatch, limit):
    monkeypatch.setattr(health, limit, 1)
    assert not cache_health(recovery_cache)["healthy"]


def test_cache_hashing_stops_at_its_deadline(recovery_cache, monkeypatch):
    ticks = iter([0, health.RECOVERY_CACHE_HASH_SECONDS])
    monkeypatch.setattr(health.time, "monotonic", lambda: next(ticks))
    assert not cache_health(recovery_cache)["healthy"]


def test_active_health_does_not_inspect_standby_cache(recovery_cache, monkeypatch):
    world = recovery_cache
    heartbeat(Queue(world.root), state="running")
    write_receipt(
        world.root / "data-backup.json",
        {
            **world.installed,
            "captured_at": (world.now - timedelta(minutes=1)).isoformat(),
        },
    )
    monkeypatch.setattr(health, "_cached_recovery", lambda *args: pytest.fail("active cache read"))
    result = cache_health(world)
    assert result["healthy"]
    assert not result["recovery_metrics"]["restore_cache"]["checked"]
