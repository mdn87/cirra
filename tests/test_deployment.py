"""Exercise the release script against disposable units, processes and SQLite state."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from cirra.store import SCHEMA_VERSION

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="production deployment uses Linux")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy-release.sh"


@pytest.fixture
def deployment(tmp_path):
    service = tmp_path / "service"
    releases, state, tools = [tmp_path / n for n in ("releases", "state", "bin")]
    for path in (service / "config", service / "staging", releases, state, tools):
        path.mkdir(parents=True)
    old, current = ["a" * 40, "b" * 40]
    for revision in (old, current):
        root = releases / revision
        (root / ".venv/bin").mkdir(parents=True)
        python = root / ".venv/bin/python"
        # The real release contains an installed package. This disposable release
        # loads the checkout with the test interpreter and uses the real SQLite driver.
        python.write_text('#!/bin/sh\n[ "$1" = -I ] && shift\nexec "$TEST_PYTHON" "$@"\n')
        python.chmod(0o755)
        (root / "examples/systemd").mkdir(parents=True)
        (root / "examples/systemd/cirra-worker.service").write_text("[Service]\n")
    (service / "current").symlink_to(releases / current)
    with sqlite3.connect(state / "cirra.sqlite3") as db:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    remote = tmp_path / "deploy.sh"
    remote.write_bytes(subprocess.check_output(["bash", str(SCRIPT), "--print-remote"]))
    for name, body in {
        "sudo": 'shift; exec "$@"',
        "curl": "echo -n 200",
        "systemctl": (
            'echo "$*" >>"$FAKE_LOG"\n'
            'case "$1:$2:$3" in\n'
            " is-active:--quiet:cirra-audit-reads.service|"
            "is-active:--quiet:cirra-health.timer) exit 3 ;;\n"
            ' is-active:--quiet:cirra-worker.service) test "$WORKER_UP" = yes ;;\n'
            " show:cirra-worker.service:--property=MainPID) echo 419 ;;\n"
            " show:cirra-worker.service:--property=InvocationID) echo new-worker ;;\n"
            " *) exit 0 ;;\nesac"
        ),
    }.items():
        path = tools / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "FAKE_LOG": str(tmp_path / "calls"),
        "TEST_PYTHON": sys.executable,
        "PYTHONPATH": str(SCRIPT.parents[1] / "src"),
        "WORKER_UP": "yes",
        "CIRRA_READY_TIMEOUT": "2",
    }

    def rollback(up=True, invocation="new-worker"):
        (state / "worker-status.json").write_text(
            json.dumps({"state": "running", "pid": 419, "invocation_id": invocation})
        )
        return subprocess.run(
            [
                "bash",
                str(remote),
                "rollback",
                old,
                "0",
                "0",
                "100",
                str(service),
                str(releases),
                str(state),
                "8796",
            ],
            env={**env, "WORKER_UP": "yes" if up else "no"},
            text=True,
            capture_output=True,
            timeout=20,
        )

    return rollback, service, state, releases / current


def test_rollback_refuses_incompatible_schema_before_switch(deployment):
    rollback, service, state, current = deployment
    with sqlite3.connect(state / "cirra.sqlite3") as db:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    result = rollback()
    assert result.returncode and "Preserve the database" in result.stderr
    assert (service / "current").resolve() == current


@pytest.mark.parametrize(
    ("up", "invocation", "success"),
    [(True, "new-worker", True), (False, "new-worker", False), (True, "previous-worker", False)],
)
def test_readiness_requires_both_units_and_current_heartbeat(deployment, up, invocation, success):
    rollback, _, _, _ = deployment
    result = rollback(up, invocation)
    assert (result.returncode == 0) == success, (result.stdout, result.stderr)
