"""The passive standby must keep its last good copy across failed refreshes."""

import importlib.util
import io
import json
import os
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def standby():
    path = Path(__file__).resolve().parents[1] / "scripts" / "standby-snapshot.py"
    spec = importlib.util.spec_from_file_location("standby_snapshot", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def receipt(char="a", hours=0):
    when = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    return dict(
        snapshot_id=char * 64,
        captured_at=when,
        verified_at=when,
        sqlite_integrity="ok",
        foreign_key_errors=0,
        schema_version=3,
        current_witnesses=2,
        historical_witnesses=3,
    )


def package(record):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("state.tar", b"fixture tar data")
        archive.writestr("verified.json", json.dumps(record))
    return stream.getvalue()


def sender(data, code=0, expected="-"):
    script = (
        "import sys; previous=sys.stdin.buffer.readline().decode().strip(); "
        f"assert previous=={expected!r}; sys.stdout.buffer.write({data!r}); "
        f"sys.exit({code})"
    )
    return [sys.executable, "-c", script]


@pytest.mark.skipif(os.name != "posix", reason="standby uses POSIX file locks and permissions")
def test_refresh_keeps_last_good_copy_and_recovers(standby, tmp_path):
    root = tmp_path / "standby"
    root.mkdir(mode=0o700)
    first = receipt(hours=2)
    data = package(first)
    assert standby.refresh(root, sender(data)) == first
    current = root / "current.zip"
    inode = current.stat().st_ino
    previous = first["snapshot_id"]
    assert standby.refresh(root, sender(b"", expected=previous)) == first
    assert current.stat().st_ino == inode
    for body, code in [
        (b"interrupted", 1),
        (data, 1),
        (data[:-36], 0),
        (package(receipt("b", hours=3)), 0),
        (package(receipt("c", hours=25)), 0),
    ]:
        with pytest.raises((ValueError, OSError, zipfile.BadZipFile)):
            standby.refresh(root, sender(body, code, previous))
        assert current.read_bytes() == data
    second = receipt("d")
    assert standby.refresh(root, sender(package(second), expected=previous)) == second
    assert standby.read_bundle(current) == second
    assert current.stat().st_mode & 0o077 == 0


def test_standby_rejects_corrupt_or_unverified_package(standby, tmp_path):
    target = tmp_path / "cache.zip"
    for changed in [
        {"snapshot_id": "latest"},
        {"sqlite_integrity": "failed"},
        {"foreign_key_errors": 1},
        {"captured_at": "tomorrow"},
    ]:
        target.write_bytes(package({**receipt(), **changed}))
        with pytest.raises(ValueError):
            standby.read_bundle(target)
    target.write_bytes(package(receipt()).replace(b"fixture tar data", b"fixture bar data"))
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        standby.read_bundle(target)


def test_export_allows_only_verified_current_snapshot(standby, tmp_path, monkeypatch):
    record = receipt()
    path = tmp_path / "verified.json"
    path.write_text(json.dumps(record))
    monkeypatch.setenv("CIRRA_APPROVED_BACKUP_REPOSITORY", "sftp:approved")
    monkeypatch.setenv("RESTIC_REPOSITORY", "sftp:approved")
    output = io.BytesIO()
    standby.export(path, io.BytesIO((record["snapshot_id"] + "\n").encode()), output)
    assert output.getvalue() == b""
    with pytest.raises(ValueError):
        standby.export(path, io.BytesIO(b"arbitrary/path\n"), output)
    monkeypatch.setenv("RESTIC_REPOSITORY", "somewhere-else")
    with pytest.raises(ValueError):
        standby.export(path, io.BytesIO(b"-\n"), output)
