import hashlib
import hmac
import importlib.util
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup-state.py"
spec = importlib.util.spec_from_file_location("cirra_state_backup", SCRIPT)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def example(tmp_path):
    import cirra.domain
    from cirra.store import Store

    state = tmp_path / "state"
    Store(state)
    observer = state / "observer"
    name = "f" * 64 + "-execution-witness"
    history = observer / "history" / name
    history.mkdir(parents=True)
    key = b"test-key" * 5
    statement = {"example": "signature", "version": 2}
    body = {
        "statement": statement,
        "hmac_sha256": hmac.new(
            key, cirra.domain.canonical(statement).encode(), hashlib.sha256
        ).hexdigest(),
    }
    path = observer / (name + ".json")
    path.write_text(json.dumps(body))
    old = history / (cirra.domain.digest(body) + ".json")
    old.write_text(json.dumps(body))
    return state.resolve(), key, path, old


@pytest.mark.skipif(
    not hasattr(__import__("os"), "O_NOFOLLOW"), reason="Linux service file semantics"
)
def test_restored_store_and_historical_signatures(tmp_path):
    state, key, current, past = example(tmp_path)
    assert backup.verify_state(state, key)["historical_witnesses"] == 1
    with pytest.raises(backup.RecoveryError, match="signature"):
        backup.verify_state(state, b"other" * 8)
    past.rename(past.with_name("0" * 64 + ".json"))
    with pytest.raises(backup.RecoveryError, match="filename"):
        backup.verify_state(state, key)
    current.write_text("{}")
    with pytest.raises(backup.RecoveryError, match="record"):
        backup.verify_state(state, key)


@pytest.mark.skipif(
    not hasattr(__import__("os"), "O_NOFOLLOW"), reason="Linux service file semantics"
)
def test_restore_rejects_inconsistent_database(tmp_path):
    state, key, _, _ = example(tmp_path)
    with sqlite3.connect(state / "cirra.sqlite3") as db:
        db.execute("INSERT INTO reports(run_id, report_digest, body) VALUES('missing','x','{}')")
    with pytest.raises(backup.RecoveryError, match="foreign-key"):
        backup.verify_state(state, key)


def test_quiescence_failure_resumes_exact_active_set(tmp_path, monkeypatch):
    journal = tmp_path / "resume.json"
    states = dict.fromkeys(backup.UNITS, "inactive")
    states.update(
        {
            "cirra-health.timer": "active",
            "cirra-webhook.service": "active",
            "cirra-worker.service": "active",
        }
    )
    monkeypatch.setattr(backup, "unit_state", lambda unit: states.get(unit, "inactive"))
    # Exercise journal/recovery even on platforms without POSIX directory fsync.
    monkeypatch.setattr(backup, "saved", lambda path, body: path.write_text(json.dumps(body)))
    calls = []

    def control(argv, timeout):
        action, unit = argv[1:]
        calls.append((action, unit))
        if action == "stop" and unit == "cirra-worker.service":
            raise backup.RecoveryError("interrupted stop")
        states[unit] = "inactive" if action == "stop" else "active"

    monkeypatch.setattr(backup, "command", control)
    with pytest.raises(backup.RecoveryError, match="interrupted"):
        with backup.quiesce(tmp_path, journal):
            pytest.fail("snapshot must not run")
    assert (
        states["cirra-worker.service"]
        == states["cirra-webhook.service"]
        == states["cirra-health.timer"]
        == "active"
    )
    assert ("start", "cirra-audit-reads.service") not in calls
    assert json.loads(journal.read_bytes()) == []


def test_busy_worker_refuses_before_any_service_stop(tmp_path):
    q = tmp_path / "queue"
    q.mkdir()
    (q / "job.json").write_text('{"state":"running"}')
    with pytest.raises(backup.RecoveryError, match="running job"):
        with backup.quiesce(tmp_path, tmp_path / "resume.json"):
            pytest.fail("snapshot must not run")
    assert not (tmp_path / "resume.json").exists()


def test_replacement_host_verification_without_a_live_database(tmp_path, monkeypatch):
    args = SimpleNamespace(
        state=tmp_path.resolve() / "lost-state",
        admin=tmp_path / "control",
        scratch=tmp_path / "isolated",
        witness_file=tmp_path / "escrow.env",
        witness_name="WITNESS",
        repository="approved",
        action="verify",
    )
    monkeypatch.setenv("RESTIC_REPOSITORY", "approved")
    monkeypatch.setattr(backup, "private_dir", lambda path: None)
    monkeypatch.setattr(backup, "restic", lambda args: b"[]")
    monkeypatch.setattr(backup, "credentials", lambda path, name: b"test")
    backup.check_target(args)
    args.action = "backup"
    with pytest.raises(backup.RecoveryError, match="source Cirra database"):
        backup.check_target(args)


@pytest.fixture
def backup_cli():
    old_mask = os.umask(0o077)
    try:
        yield backup.main
    finally:
        os.umask(old_mask)


@pytest.mark.skipif(os.name != "posix", reason="Linux deployment flock")
@pytest.mark.parametrize("action", ["backup", "resume"])
def test_pending_restart_waits_for_deployment_and_retries(
    tmp_path, monkeypatch, backup_cli, action
):
    import fcntl

    admin = tmp_path / "control"
    admin.mkdir(mode=0o700)
    journal = admin / "resume.json"
    unit = "cirra-worker.service"
    journal.write_bytes(json.dumps([unit]).encode())
    previous = journal.read_bytes()
    deploy_lock = tmp_path / "deploy.lock"
    deploy_lock.touch()
    args = [action, "--admin", str(admin), "--deploy-lock", str(deploy_lock)]
    calls = []

    def systemctl(argv, timeout=30):
        calls.append(argv)
        with deploy_lock.open("rb") as competitor, pytest.raises(BlockingIOError):
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return b"active" if argv[1] == "show" else b""

    def unavailable_repository(args):
        raise backup.RecoveryError("repository unavailable")

    monkeypatch.setattr(backup, "command", systemctl)
    monkeypatch.setattr(backup, "check_target", unavailable_repository)
    with deploy_lock.open("rb") as deployment:
        fcntl.flock(deployment, fcntl.LOCK_EX)
        assert backup_cli(args) == 1
        assert calls == [] and journal.read_bytes() == previous

    # Even a repository outage on retry must allow the interrupted units to resume.
    assert backup_cli(args) == (1 if action == "backup" else 0)
    assert calls[0] == ["systemctl", "start", unit]
    assert len(calls) == 2 and json.loads(journal.read_bytes()) == []


@pytest.mark.parametrize("queue", ["queue", "audit-queue"])
def test_quiescence_preserves_unrunnable_records_and_detects_running_work(tmp_path, queue):
    directory = tmp_path / queue
    directory.mkdir()
    contents = [b"{truncated", b"null", b"[]", b"\xff", b"[" * 2000 + b"]" * 2000]
    paths = []
    for index, raw in enumerate(contents):
        path = directory / f"{index}.json"
        path.write_bytes(raw)
        paths.append(path)
    job = directory / "job.json"
    job.write_bytes(b'{"state":"running"}')
    with pytest.raises(backup.RecoveryError, match="running job"):
        backup.require_idle(tmp_path)
    job.write_bytes(b'{"state":"done"}')
    backup.require_idle(tmp_path)
    assert [path.read_bytes() for path in paths] == contents


def test_quiescence_refuses_an_unreadable_queue_entry(tmp_path):
    directory = tmp_path / "queue" / "bad.json"
    directory.mkdir(parents=True)
    with pytest.raises((OSError, backup.RecoveryError)):
        backup.require_idle(tmp_path)


def test_quiescence_propagates_queue_directory_io_error(tmp_path, monkeypatch):
    def denied(directory):
        raise PermissionError("unreadable queue")

    monkeypatch.setattr(backup.os, "scandir", denied)
    with pytest.raises(PermissionError):
        backup.require_idle(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="Linux deployment flock and directory fsync")
def test_quiescent_snapshot_includes_malformed_queue_evidence(tmp_path, monkeypatch, backup_cli):
    state = tmp_path / "state"
    admin = tmp_path / "control"
    admin.mkdir(mode=0o700)
    deploy_lock = tmp_path / "deploy.lock"
    deploy_lock.touch()
    for directory in ("queue", "audit-queue"):
        (state / directory).mkdir(parents=True)
        (state / directory / "damaged.json").write_bytes(b"[broken")
    worker = "cirra-worker.service"
    states = dict.fromkeys(backup.UNITS, "inactive")
    states[worker] = "active"
    snapshot = "f" * 64
    captured = []

    def control(argv, timeout):
        action, unit = argv[1:]
        states[unit] = "inactive" if action == "stop" else "active"

    def restic(argv, *, timeout):
        assert argv[0] == "backup" and argv[-1] == str(state)
        assert all(s == "inactive" for s in states.values())
        captured.extend(path.read_bytes() for path in state.glob("*/damaged.json"))
        return json.dumps({"message_type": "summary", "snapshot_id": snapshot}).encode()

    def verify(args, result):
        assert states[worker] == "active" and result["snapshot_id"] == snapshot

    monkeypatch.setattr(backup, "check_target", lambda args: None)
    monkeypatch.setattr(backup, "unit_state", lambda unit: states.get(unit, "inactive"))
    monkeypatch.setattr(backup, "command", control)
    monkeypatch.setattr(backup, "restic", restic)
    monkeypatch.setattr(backup, "verify", verify)
    args = [
        "backup",
        "--state",
        str(state),
        "--admin",
        str(admin),
        "--deploy-lock",
        str(deploy_lock),
    ]
    assert backup_cli(args) == 0
    assert captured == [b"[broken", b"[broken"]
