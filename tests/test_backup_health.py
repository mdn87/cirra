import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("backup_health", ROOT / "scripts/backup-health.py")
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def proof(directory, captured, verified, filename="last-verified.json"):
    (directory / filename).write_text(
        json.dumps(
            {
                "captured_at": captured.isoformat(),
                "verified_at": verified.isoformat(),
                "sqlite_integrity": "ok",
                "foreign_key_errors": 0,
                "snapshot_id": "private-fixture-value",
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def manager(monkeypatch):
    props = {"LoadState": "loaded", "Result": "success"}

    def run(argv, **kwargs):
        assert argv[:3] == ["/usr/bin/systemctl", "show", "cirra-state-backup.service"]
        assert kwargs["check"] and kwargs["timeout"] == 5
        return subprocess.CompletedProcess(argv, 0, "\n".join(f"{k}={v}" for k, v in props.items()))

    monkeypatch.setattr(monitor.subprocess, "run", run)
    return props


def test_missing_stale_and_fresh_verified_capture(tmp_path, manager):
    now = datetime.now(UTC)
    proof(tmp_path, now, now, filename="last-drill.json")
    proof(tmp_path, now, now, filename="last-backup.json")
    assert "missing" in monitor.problem(tmp_path, now)
    boundary = now - timedelta(hours=26)
    proof(tmp_path, boundary - timedelta(seconds=1), now)
    assert "over 26 hours" in monitor.problem(tmp_path, now)
    proof(tmp_path, boundary, now)
    assert monitor.problem(tmp_path, now) is None
    manager["Result"] = "exit-code"
    assert "service" in monitor.problem(tmp_path, now)
    manager.update(LoadState="not-found", Result="success")
    assert "service" in monitor.problem(tmp_path, now)


def test_failed_backup_stays_visible_until_a_new_capture_is_verified(
    tmp_path, manager, monkeypatch, capsys
):
    now = datetime.now(UTC)
    failed = now - timedelta(minutes=15)
    proof(tmp_path, now - timedelta(hours=24), now - timedelta(hours=23))
    monkeypatch.setenv("SERVICE_RESULT", "timeout")
    assert monitor.main(["record-result"], tmp_path) == 0
    marker = tmp_path / "last-failure"
    if os.name == "posix":
        assert marker.stat().st_mode & 0o077 == 0
    os.utime(marker, (failed.timestamp(), failed.timestamp()))
    # systemd resets its current Result on a retry. The verified capture is still yesterday's.
    assert monitor.main(["check"], tmp_path) == 1
    event = json.loads(capsys.readouterr().out)
    assert event == {
        "healthy": False,
        "problems": [
            {
                "kind": "backup",
                "detail": "backup failed and no newer capture has passed restore verification",
            }
        ],
    }
    monkeypatch.setenv("SERVICE_RESULT", "success")
    assert monitor.main(["record-result"], tmp_path) == 0
    proof(tmp_path, now - timedelta(hours=24), now)
    assert monitor.problem(tmp_path, now) == event["problems"][0]["detail"]
    proof(tmp_path, now - timedelta(minutes=5), now)
    assert monitor.main(["check"], tmp_path) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("content", [b"private-fixture-value", b" " * 8193, b"{}", b"null"])
def test_corrupt_evidence_emits_only_a_sanitized_reason(tmp_path, content, capsys):
    (tmp_path / "last-verified.json").write_bytes(content)
    assert monitor.main(["check"], tmp_path) == 1
    assert json.loads(capsys.readouterr().out)["problems"] == [
        {"kind": "backup", "detail": "cannot validate backup health evidence"}
    ]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Linux backup service")
def test_special_file_is_rejected_without_waiting_for_a_writer(tmp_path):
    os.mkfifo(tmp_path / "last-verified.json")
    assert monitor.problem(tmp_path, datetime.now(UTC)) == "cannot validate backup health evidence"


def test_backup_health_is_optional_and_shares_the_existing_alert_cycle():
    units = ROOT / "examples/systemd"
    health = (units / "cirra-health.service").read_text(encoding="utf-8")
    assert "backup" not in health
    assert "OnFailure=cirra-health-alert.service" in health
    assert "OnSuccess=cirra-health-resolve.service" in health
    monitor_unit = (units / "cirra-health.service.d/state-backup.conf").read_text(encoding="utf-8")
    assert (
        "ExecStartPost=!/usr/bin/python3 -I /usr/local/libexec/cirra-backup-health.py check"
        in monitor_unit
    )
    service = (units / "cirra-state-backup.service").read_text(encoding="utf-8")
    assert "OnFailure=cirra-health.service" in service
    assert "OnSuccess=cirra-health.service" in service
    record = (
        "ExecStopPost=-/usr/bin/python3 -I /usr/local/libexec/cirra-backup-health.py record-result"
    )
    resume = "ExecStopPost=/usr/bin/python3 -I /usr/local/libexec/cirra-backup-state.py resume"
    assert service.index(record) < service.index(resume)
