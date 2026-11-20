import hashlib
import hmac
import importlib.util
import json
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
