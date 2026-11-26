import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from test_audit_reads import get

from cirra.audit_reads import AuditReadServer
from cirra.store import Store

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/managed-linux-state.sh"
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="managed Linux systemd layout")


def select(parent, version):
    link = parent / ".next"
    link.symlink_to(Path("current.restores") / version)
    os.replace(link, parent / "current")


def test_mounted_reader_refuses_switched_results_until_rebound(tmp_path, monkeypatch):
    versions = tmp_path / "current.restores"
    first, second = versions / "seed", versions / ("a" * 64)
    Store(first)
    Store(second)
    select(tmp_path, first.name)
    monkeypatch.setenv("CIRRA_AUDIT_STATE_SELECTOR", str(tmp_path / "current"))
    with AuditReadServer(("127.0.0.1", 0), first) as reader:
        thread = threading.Thread(target=reader.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            assert get(reader, "/healthz")[0].status == 200
            read = reader.snapshot.read

            def read_then_switch(*args):
                response = read(*args)
                select(tmp_path, second.name)
                return response

            monkeypatch.setattr(reader.snapshot, "read", read_then_switch)
            assert get(reader, "/healthz")[0].status == 503
            assert get(reader, "/audits")[0].status == 503
        finally:
            reader.shutdown()
            thread.join(timeout=2)
    with AuditReadServer(("127.0.0.1", 0), second) as rebound:
        assert rebound.read("/healthz")[0] == 200


def test_backup_spool_belongs_to_the_actual_selected_private_version(tmp_path):
    version = "a" * 64
    physical = tmp_path / "current.restores" / version
    physical.mkdir(parents=True)
    select(tmp_path, version)

    def prepare(selected):
        return subprocess.run(["bash", SCRIPT, "spool", tmp_path, selected], check=False)

    assert prepare("seed").returncode
    assert prepare(version).returncode == 0
    spool = physical.with_name(version + ".private-transfer")
    assert spool.is_dir() and spool.stat().st_uid == os.geteuid()
    assert spool.stat().st_mode & 0o777 == 0o700
    assert not (tmp_path / "current.private-transfer").exists()
    spool.chmod(0o750)
    assert prepare(version).returncode
    (tmp_path / ".outside").symlink_to(tmp_path.parent)
    (tmp_path / ".outside").replace(tmp_path / "current")
    assert prepare(version).returncode


@pytest.mark.skipif(not shutil.which("setfacl"), reason="POSIX ACL tools required")
def test_reader_rebind_revokes_traversal_of_the_previous_state(tmp_path):
    versions = tmp_path / "current.restores"
    old, new = versions / "seed", versions / ("b" * 64)
    Store(old)
    Store(new)
    checkpoint = tmp_path / "saved-reader-version"
    for state in (old, new):
        select(tmp_path, state.name)
        subprocess.run(["bash", SCRIPT, "reader", tmp_path, checkpoint, "nobody"], check=True)
    assert checkpoint.read_text().strip() == new.name
    for state, access in [(old, "---"), (new, "--x")]:
        listing = subprocess.check_output(["getfacl", "-cp", state], text=True)
        assert f"user:nobody:{access}" in listing.splitlines()
