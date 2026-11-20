"""Exercise the release script against disposable units, processes and SQLite state."""

import json
import os
import sqlite3
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from cirra.store import SCHEMA_VERSION

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="production deployment uses Linux")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy-release.sh"


@pytest.fixture
def deployment(tmp_path):
    service = tmp_path / "service"
    releases, state = tmp_path / "releases", tmp_path / "state"
    for path in (service / "config", service / "staging", releases, state):
        path.mkdir(parents=True)
    old, current = ["a" * 40, "b" * 40]
    for revision in (old, current):
        root = releases / revision
        runtime = root / ".venv"
        venv.EnvBuilder(symlinks=True, with_pip=False, system_site_packages=True).create(runtime)
        site = next((runtime / "lib").glob("python*/site-packages"))
        (site / "cirra.pth").write_text(str(SCRIPT.parents[1] / "src") + "\n")
        (root / "examples/systemd").mkdir(parents=True)
        (root / "examples/systemd/cirra-worker.service").write_text("[Service]\n")
    (service / "current").symlink_to(releases / current)
    with sqlite3.connect(state / "cirra.sqlite3") as db:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    remote = tmp_path / "deploy.sh"
    remote.write_bytes(subprocess.check_output(["bash", str(SCRIPT), "--print-remote"]))
    stubs = tmp_path / "units.bash"
    stubs.write_text(
        'sudo() { shift; "$@"; }\n'
        "curl() { echo -n 200; }\n"
        "systemctl() {\n"
        'echo "$*" >>"$FAKE_LOG"\n'
        'case "$1:${2-}:${3-}" in\n'
        " is-active:--quiet:cirra-audit-reads.service|"
        "is-active:--quiet:cirra-health.timer) return 3 ;;\n"
        ' is-active:--quiet:cirra-worker.service) test "$WORKER_UP" = yes ;;\n'
        " show:cirra-worker.service:--property=MainPID) echo 419 ;;\n"
        " show:cirra-worker.service:--property=InvocationID) echo new-worker ;;\n"
        " *) return 0 ;;\nesac\n}\n"
    )
    env = {
        **os.environ,
        "BASH_ENV": str(stubs),
        "FAKE_LOG": str(tmp_path / "calls"),
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
    if not success:
        assert "post-restart readiness check" in result.stderr
