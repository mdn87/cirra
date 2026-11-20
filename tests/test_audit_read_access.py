"""Exercise the installed ACLs against Linux SQLite's live rollback journal."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from http.client import HTTPConnection
from pathlib import Path

import pytest

from cirra.publish import write_receipt
from cirra.store import Store

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/grant-audit-read-access.sh"


@pytest.mark.skipif(
    sys.platform != "linux" or os.getuid() != 0 or not shutil.which("setfacl"),
    reason="requires Linux root and POSIX ACL tools to exercise a separate unprivileged UID",
)
def test_reader_account_sees_commits_without_write_or_history_access():
    with tempfile.TemporaryDirectory(prefix="cirra-reader-acl-") as root:
        base = Path(root)
        base.chmod(0o711)
        state = base / "state"
        store = Store(state)
        state.chmod(0o700)
        store.path.chmod(0o600)
        private = state / "worker-private"
        private.write_text("private", encoding="utf8")
        private.chmod(0o600)
        publication = state / "audit-publication" / ("a" * 64 + ".json")
        write_receipt(publication, {"gate": "pass"})
        subprocess.run(["bash", str(SCRIPT), str(state), "nobody"], check=True)
        child = """
import os, pwd, sys
from pathlib import Path
from cirra.audit_reads import AuditReadServer
account = pwd.getpwnam('nobody')
os.setgroups([])
os.setgid(account.pw_gid)
os.setuid(account.pw_uid)
root = Path(sys.argv[1])
assert not os.access(root, os.W_OK)
assert not os.access(root / 'cirra.sqlite3', os.W_OK)
assert not os.access(root / 'worker-private', os.R_OK)
assert not os.access(root / 'audit-publication' / 'history', os.X_OK)
assert not os.access(root / 'audit-publication', os.W_OK)
with AuditReadServer(('127.0.0.1', 0), root) as server:
    print(server.server_port, flush=True)
    for _ in sys.stdin:
        server.handle_request()
"""
        proc = subprocess.Popen(
            [os.environ.get("CIRRA_TEST_READER_PYTHON", sys.executable), "-c", child, str(state)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        try:
            port = int(proc.stdout.readline())

            def read(path):
                proc.stdin.write("read\n")
                proc.stdin.flush()
                client = HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    client.request("GET", path)
                    response = client.getresponse()
                    assert response.status == 200
                    return json.loads(response.read())["record"]
                finally:
                    client.close()

            with store.transaction() as db:
                db.execute(
                    "INSERT INTO audits(attempt_id,audit_id,number,repository_key,tier,head,state,"
                    "identity_json,created_at,scope) VALUES('a','b',1,'r','pr','h','prepared',"
                    "'{}',0,'repository')"
                )
                assert read("/healthz")["audits"] == 0
            assert read("/healthz")["audits"] == 1
            route = "/gates/" + "a" * 64
            assert read(route)["publication"] == {"gate": "pass"}
            write_receipt(publication, {"gate": "block"})
            assert read(route)["publication"] == {"gate": "block"}
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)
        assert proc.returncode == 0
