import hashlib
import hmac
import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
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
        witness_socket=None,
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


@pytest.mark.skipif(os.name != "posix", reason="Linux recovery directory and lock semantics")
def test_bws_only_restore_checks_witness_without_leaking_it(tmp_path, monkeypatch, backup_cli):
    admin, scratch = tmp_path / "admin", tmp_path / "scratch"
    admin.mkdir(mode=0o700)
    scratch.mkdir(mode=0o700)
    snapshot = "1" * 64
    state = tmp_path / "lost"
    key = b"test-key" * 5
    monkeypatch.setenv("RESTIC_REPOSITORY", "approved")

    def restic(argv, **kwargs):
        assert "CIRRA_WITNESS_KEY" not in os.environ
        if argv == ["snapshots", snapshot]:
            return json.dumps(
                [
                    {
                        "id": snapshot,
                        "tags": [backup.TAG],
                        "paths": [str(state)],
                        "time": "2026-01-01T00:00:00+00:00",
                    }
                ]
            ).encode()
        if argv[0] == "restore":
            target = Path(argv[-1]) / state.relative_to("/")
            target.parent.mkdir(parents=True)
            fixture, _, _, _ = example(target.parent)
            fixture.rename(target)
        return b"[]"

    monkeypatch.setattr(backup, "restic", restic)
    args = [
        "verify",
        "--state",
        str(state),
        "--scratch",
        str(scratch),
        "--admin",
        str(admin),
        "--repository",
        "approved",
        "--snapshot",
        snapshot,
        "--witness-socket",
        str(tmp_path / "w.sock"),
    ]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(tmp_path / "w.sock"))
        (tmp_path / "w.sock").chmod(0o600)
        listener.listen()
        listener.settimeout(5)

        def send_key():
            for value in (key, b""):
                conn, _ = listener.accept()
                with conn:
                    conn.sendall(value)

        provider = threading.Thread(target=send_key, daemon=True)
        provider.start()
        assert backup_cli(args) == 0
        assert backup_cli(args) == 1
        provider.join(5)
        assert not provider.is_alive()
    proof = json.loads((admin / "last-drill.json").read_text())
    assert proof["sqlite_integrity"] == "ok"
    assert proof["current_witnesses"] == proof["historical_witnesses"] == 1
    assert 0 < proof["restore_space"]["free_bytes"] <= proof["restore_space"]["total_bytes"]
    assert key.decode() not in json.dumps(proof)


def test_backup_unit_uses_dedicated_witness_runtime():
    root = SCRIPT.parents[1]
    service = (root / "examples/systemd/cirra-state-backup.service").read_text()
    units = root / "examples/systemd"
    binding = (units / "cirra-state-backup.service.d/bws-witness.conf").read_text()
    provider = (units / "backup/cirra-backup-witness@.service").read_text()
    listener = (units / "backup/cirra-backup-witness.socket").read_text()
    runtime = json.loads((root / "examples/glas/cirra-backup.json").read_text())
    assert "Requires=cirra-backup-witness.socket" in binding
    assert "--witness-socket /run/cirra-backup-witness.sock" in binding
    assert "LoadCredentialEncrypted=" not in service
    assert "ListenStream=/run/cirra-backup-witness.sock" in listener
    assert "SocketMode=0600" in listener and "SocketUser=root" in listener
    assert "User=matt" in provider and "StandardInput=socket" in provider
    assert "LoadCredentialEncrypted=glas-bws-token:" in provider
    assert "/etc/cirra/cirra-backup.json" in provider
    assert runtime["home"] == "/var/lib/cirra-backup-glas"
    assert runtime["manifest"].endswith("/cirra-backup.manifest.json")
    assert runtime["command"] == [
        "/usr/bin/python3",
        "-I",
        "/usr/local/libexec/cirra-backup-witness.py",
    ]
    assert (
        "ExecStopPost=/usr/bin/python3 -I /usr/local/libexec/cirra-backup-state.py resume"
        in service
    )


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Linux Unix socket credentials")
def test_witness_provider_checks_the_actual_peer_and_keeps_stdout_empty():
    key = "fixture-signing-key" * 3
    client, server = socket.socketpair()
    with client, server:
        result = subprocess.run(
            [sys.executable, "-I", str(SCRIPT.with_name("backup-witness.py"))],
            stdin=server,
            capture_output=True,
            env={**os.environ, "CIRRA_WITNESS_KEY": key},
            timeout=10,
        )
        server.close()
        assert result.stdout == b"" and key.encode() not in result.stderr
        assert result.returncode == (0 if os.geteuid() == 0 else 1)
        assert client.recv(100) == (key.encode() if os.geteuid() == 0 else b"")


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
