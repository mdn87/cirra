"""Exercise the release script against disposable units, processes and SQLite state."""

import json
import os
import shlex
import shutil
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
        ' restart:*) touch "$FAKE_LOG.restarted" ;;\n'
        " show:*:--property=FragmentPath) "
        'test "$FAIL_RESTORE" != yes || test ! -e "$FAKE_LOG.restarted" ;;\n'
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

    def rollback(up=True, invocation="new-worker", fail_restore=False):
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
            env={
                **env,
                "WORKER_UP": "yes" if up else "no",
                "FAIL_RESTORE": "yes" if fail_restore else "no",
            },
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


@pytest.mark.parametrize("live", ["marker", "archived", "awaiting_publication"])
def test_rollback_refuses_older_queue_readers_before_switch(deployment, live):
    from cirra.queue_format import activate

    rollback, service, state, current = deployment
    source = current / "src/cirra"
    source.mkdir(parents=True)
    shutil.copyfile(SCRIPT.parents[1] / "src/cirra/queue_format.py", source / "queue_format.py")
    (current / "worker-state-version").write_text("1\n")
    if live == "marker":
        activate(state)
    elif live == "archived":
        (state / "queue/completed").mkdir(parents=True)
    else:
        (state / "audit-queue").mkdir()
        (state / "audit-queue/job.json").write_text('{"state":"awaiting_publication"}')
    result = rollback()
    assert (
        result.returncode and "live worker queue format 1 exceeds release format 0" in result.stderr
    )
    assert (service / "current").resolve() == current
    with sqlite3.connect(state / "cirra.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_persistent_guard_protects_both_original_units_with_old_release_selected(tmp_path):
    from cirra.queue_format import activate

    service, state, units = [tmp_path / name for name in ("service", "state", "units")]
    old, new = service / ("a" * 40), service / ("b" * 40)
    for directory in (old, new / "src/cirra", service / "staging", state, units):
        directory.mkdir(parents=True)
    shutil.copyfile(
        SCRIPT.parents[1] / "src/cirra/queue_format.py", new / "src/cirra/queue_format.py"
    )
    (new / "worker-state-version").write_text("1\n")
    (service / "current").symlink_to(new)
    remote = subprocess.check_output(["bash", str(SCRIPT), "--print-remote"], text=True)
    functions = remote.split('if [ "$MODE" = rollback ]; then')[0]
    command = functions + '\ninstall_queue_guard "$SERVICE/current" "$R"\n'
    stubs = tmp_path / "fixture-units.sh"
    stubs.write_text(
        'sudo() { shift; "$@"; }\n'
        'systemctl() { if [ "$1" = show ]; then echo "/etc/systemd/system/$2"; fi; }\n'
    )
    subprocess.run(
        [
            "bash",
            "-c",
            command,
            "fixture",
            "deploy",
            new.name,
            "0",
            "0",
            "3",
            str(service),
            str(service),
            str(state),
            "8796",
        ],
        check=True,
        env={**os.environ, "BASH_ENV": str(stubs), "CIRRA_SYSTEMD_UNIT_DIR": str(units)},
    )
    activate(state)
    guards = sorted(units.glob("*.d/*.conf"))
    assert {p.parent.name for p in guards} == {"cirra-worker.service.d", "cirra-webhook.service.d"}
    args = shlex.split(guards[0].read_text().split("ExecStartPre=")[1])
    assert subprocess.run(args).returncode == 0
    (service / "current").unlink()
    (service / "current").symlink_to(old)
    guarded = subprocess.run(args, text=True, capture_output=True)
    assert guarded.returncode and "release format 0" in guarded.stderr


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


def test_failed_unit_restore_still_restores_release_and_configuration(deployment):
    rollback, service, _, current = deployment
    (service / "config/setting").write_text("current")
    previous = service / "staging" / ("config-before-" + current.name)
    previous.mkdir()
    (previous / "setting").write_text("previous")
    result = rollback(up=False, fail_restore=True)
    assert result.returncode and "Could not restore every managed unit" in result.stderr
    assert (service / "current").resolve() == current
    assert (service / "config/setting").read_text() == "current"
