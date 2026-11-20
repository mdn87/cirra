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
        ' show:cirra-worker.service:--property=MainPID) echo "$WORKER_MAIN_PID" ;;\n'
        " show:cirra-worker.service:--property=InvocationID) echo new-worker ;;\n"
        " *) return 0 ;;\nesac\n}\n"
    )
    env = {
        **os.environ,
        "BASH_ENV": str(stubs),
        "FAKE_LOG": str(tmp_path / "calls"),
        "WORKER_UP": "yes",
        "WORKER_MAIN_PID": str(os.getpid()),
        "CIRRA_READY_TIMEOUT": "2",
    }

    def rollback(up=True, invocation="new-worker", fail_restore=False, worker_pid=None):
        (state / "worker-status.json").write_text(
            json.dumps(
                {"state": "running", "pid": worker_pid or os.getpid(), "invocation_id": invocation}
            )
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


def test_launcher_child_must_belong_to_the_current_worker_unit(deployment):
    rollback, _, _, _ = deployment
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) as child:
        try:
            result = rollback(worker_pid=child.pid)
            assert result.returncode == 0, result.stderr
        finally:
            child.terminate()


def test_unrelated_process_cannot_satisfy_worker_readiness(deployment):
    rollback, _, _, _ = deployment
    result = rollback(worker_pid=os.getppid())
    assert result.returncode and "post-restart readiness check" in result.stderr


@pytest.fixture
def format_transition(deployment):
    """Prepare three distinct binaries for a first rollout, with real package and pin checks."""
    import yaml

    from cirra.pins import check_pins

    _, service, state, previous = deployment
    releases = previous.parent
    candidate = releases / ("a" * 40)
    fallback = releases / ("c" * 40)
    shutil.copytree(candidate, fallback, symlinks=True)
    project = SCRIPT.parents[1]
    for root in (candidate, fallback):
        shutil.copytree(
            project / "src/cirra", root / "src/cirra", ignore=shutil.ignore_patterns("__pycache__")
        )
        site = next((root / ".venv/lib").glob("python*/site-packages"))
        (site / "cirra.pth").write_text(
            f"{root / 'src'}\n{Path(yaml.__file__).parents[1]}\n", encoding="utf-8"
        )
        (root / "worker-state-version").write_text("1\n")
        (root / ".installation-complete").touch()
    launcher = candidate / "src/cirra/observer_runner.py"
    launcher.write_bytes(launcher.read_bytes() + b"\n# Candidate observer implementation.\n")
    config = service / "config"
    for name in ("service.yaml", "github-worker-policy.yaml", "observer.yaml"):
        shutil.copyfile(project / "examples" / name, config / name)
    policy = config / "github-worker-policy.yaml"
    check_pins(config / "service.yaml", repin=True)
    original = policy.read_bytes()

    # systemctl activates the real candidate queue writer before it reports a startup failure.
    stubs = service.parent / "transition.bash"
    stubs.write_text(
        'sudo() { shift; "$@"; }\n'
        "curl() { echo -n 200; }\n"
        "systemctl() {\n"
        '  case "$1:$2" in\n'
        "    restart:*)\n"
        '      local selected; selected=$(readlink -f "$TEST_SERVICE/current")\n'
        '      basename "$selected" >> "$TEST_STAGING/restarts"\n'
        '      if [ "$selected" = "$TEST_CANDIDATE" ]; then\n'
        '        cp "$TEST_SERVICE/config/github-worker-policy.yaml" '
        '"$TEST_STAGING/candidate-policy"\n'
        '        if [ "$TEST_ACTIVATE" = yes ]; then\n'
        '          "$selected/.venv/bin/python" -I -B -c '
        "'from pathlib import Path; import sys; from cirra.queue_format import activate; "
        'activate(Path(sys.argv[1]))\' "$TEST_STATE" || return 1\n'
        "        fi\n"
        "        return 1\n"
        "      fi\n"
        '      printf \'{"state":"running","pid":%s,"invocation_id":"restored",'
        '"heartbeat_at":"%s"}\\n\' '
        '"$TEST_PID" "$(date -u +%FT%TZ)" > "$TEST_STATE/worker-status.json" ;;\n'
        "    show:*)\n"
        '      case "$3" in --property=MainPID) echo "$TEST_PID";; '
        "--property=InvocationID) echo restored;; esac ;;\n"
        '    is-active:*) case "$3" in '
        "cirra-audit-reads.service|cirra-health.timer) return 3;; esac ;;\n"
        "  esac\n}\n",
        encoding="utf-8",
    )
    staging = service / "staging"

    def deploy(activate=True):
        result = subprocess.run(
            [
                "bash",
                str(service.parent / "deploy.sh"),
                "deploy",
                candidate.name,
                "1",
                "0",
                "3",
                str(service),
                str(releases),
                str(state),
                "8796",
                fallback.name,
            ],
            env={
                **os.environ,
                "BASH_ENV": str(stubs),
                "TEST_SERVICE": str(service),
                "TEST_STAGING": str(staging),
                "TEST_STATE": str(state),
                "TEST_CANDIDATE": str(candidate),
                "TEST_ACTIVATE": "yes" if activate else "no",
                "TEST_PID": str(os.getpid()),
                "CIRRA_READY_TIMEOUT": "2",
            },
            capture_output=True,
            text=True,
            timeout=25,
        )
        assert result.returncode != 0
        return result

    return deploy, service, state, previous, candidate, fallback, policy, original


@pytest.mark.parametrize("activate", [False, True])
def test_failed_first_rollout_restores_the_newest_compatible_binary(format_transition, activate):
    deploy, service, state, previous, candidate, fallback, policy, original = format_transition
    result = deploy(activate)
    expected = fallback if activate else previous
    assert (service / "current").resolve() == expected, result.stderr
    assert (service / "staging/restarts").read_text().splitlines() == [
        candidate.name,
        expected.name,
    ]
    assert (service / "staging/candidate-policy").read_bytes() != original
    assert policy.read_bytes() == original
    assert json.loads((state / "worker-status.json").read_text())["state"] == "running"
    assert "Deployment recovery requires attention" not in result.stderr
    if activate:
        assert "live worker queue format 1 exceeds release format 0" in result.stderr
        assert '"healthy": true' in result.stdout
        assert json.loads((state / "worker-queue-format.json").read_text()) == {"format": 1}


@pytest.mark.parametrize(
    ("unusable", "message"),
    [
        ("unfinished", "no finished installation"),
        ("format", "cannot read candidate format"),
        ("sqlite", "cannot read the candidate SQLite schema"),
        ("pins", "cannot verify the saved service configuration and observer pins"),
        ("external", "cannot restore an externally stored policy changed by --repin"),
    ],
)
def test_invalid_fallback_refuses_before_changing_active_release(
    format_transition, unusable, message
):
    deploy, service, state, previous, _, fallback, policy, original = format_transition
    if unusable == "unfinished":
        (fallback / ".installation-complete").unlink()
    elif unusable == "format":
        (fallback / "worker-state-version").write_text("0\n")
    elif unusable == "sqlite":
        source = fallback / "src/cirra/store.py"
        source.write_text(source.read_text().replace("SCHEMA_VERSION = 3", "SCHEMA_VERSION = 4"))
    elif unusable == "external":
        external = service.parent / "external-policy.yaml"
        shutil.copyfile(policy, external)
        definition = service / "config/service.yaml"
        definition.write_text(definition.read_text().replace(policy.name, str(external)))
    else:
        launcher = fallback / "src/cirra/observer_runner.py"
        launcher.write_bytes(launcher.read_bytes() + b"\n# Different fallback observer.\n")
    result = deploy()
    assert message in result.stderr, result.stderr
    assert (service / "current").resolve() == previous
    assert policy.read_bytes() == original
    assert not (service / "staging/restarts").exists()
    assert not (state / "worker-queue-format.json").exists()
