"""Exercise HA file switches and rollback against disposable native filesystem state."""

import base64
import hashlib
import io
import json
import runpy
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

DEPLOY = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/deploy-ha.py"))


class Controller:
    def __init__(self):
        self.running = False
        self.restarts = 0
        self.fail = False

    def active(self, role):
        return self.running

    def restart_receivers(self):
        self.restarts += 1

    def ready(self):
        if self.fail and self.restarts == 1:
            raise RuntimeError("candidate cannot start")


@pytest.fixture
def installation(tmp_path):
    if sys.platform == "win32":
        pytest.skip("native symlink deployment runs on Linux and macOS")
    old, new = (tmp_path / name for name in ("old", "new"))
    old.mkdir()
    new.mkdir()
    config = tmp_path / "service.yaml"
    config.write_bytes(b"original config")
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(f'RELEASE = Path("{old}")\n')
    p = SimpleNamespace(
        host="isolated",
        config=config,
        wrapper=wrapper,
        current=tmp_path / "current",
        journals=tmp_path / "journals",
    )
    files = {config: b"new config", wrapper: b"new wrapper", tmp_path / "intake.yaml": b"baseline"}
    checked = []

    def check(profile, release):
        checked.append(release)
        return {"state_version": "unchanged"}

    return p, old, new, files, Controller(), checked, check


def test_switch_and_rollback_preserve_config_and_retain_new_files(installation):
    p, old, new, files, control, checked, check = installation
    result = DEPLOY["activate"](p, new, files, control, peer_healthy=True, check=check)
    assert p.current.resolve() == new
    assert p.config.read_bytes() == b"new config"
    journal = p.journals / result["transaction"] / "transaction.json"
    assert json.loads(journal.read_bytes())["phase"] == "verified"
    DEPLOY["restore"](p, journal, control, check=check)
    assert p.config.read_bytes() == b"original config"
    assert not p.current.exists()
    assert (journal.parent / "retained-new-file-2").read_bytes() == b"baseline"
    assert checked == [new, old, old]
    assert control.restarts == 2


def test_failed_readiness_automatically_restores_previous_runtime(installation):
    p, old, new, files, control, checked, check = installation
    p.current.symlink_to(old, target_is_directory=True)
    control.fail = True
    with pytest.raises(RuntimeError, match="candidate cannot start"):
        DEPLOY["activate"](p, new, files, control, peer_healthy=True, check=check)
    assert p.current.resolve() == old
    assert p.config.read_bytes() == b"original config"
    assert control.restarts == 2
    journal = next(p.journals.glob("*/transaction.json"))
    assert json.loads(journal.read_bytes())["phase"] == "rolled_back"


@pytest.mark.parametrize("cause", ["peer", "worker", "schema", "unfinished"])
def test_activation_refuses_missing_safety_evidence_before_mutation(installation, cause):
    p, old, new, files, control, checked, check = installation
    control.running = cause == "worker"
    if cause == "schema":

        def check(*args):
            raise ValueError("incompatible schema")

    if cause == "unfinished":
        path = p.journals / "earlier" / "transaction.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"phase":"applied"}')
    with pytest.raises(ValueError):
        DEPLOY["activate"](p, new, files, control, peer_healthy=cause != "peer", check=check)
    assert p.config.read_bytes() == b"original config"
    assert not p.current.exists()
    assert control.restarts == 0


def test_rollback_refuses_newer_state_without_touching_runtime(installation):
    p, old, new, files, control, checked, check = installation
    result = DEPLOY["activate"](p, new, files, control, peer_healthy=True, check=check)

    def incompatible(*args):
        raise ValueError("queue format exceeds release format")

    with pytest.raises(ValueError, match="queue format"):
        DEPLOY["restore"](
            p, p.journals / result["transaction"] / "transaction.json", control, check=incompatible
        )
    assert p.current.resolve() == new
    assert p.config.read_bytes() == b"new config"
    assert control.restarts == 1


def test_rollback_requires_latest_active_transaction_and_refuses_replay(installation):
    p, old, new, files, control, checked, check = installation
    first = DEPLOY["activate"](p, new, files, control, peer_healthy=True, check=check)
    third = new.with_name("third")
    third.mkdir()
    second = DEPLOY["activate"](p, third, files, control, peer_healthy=True, check=check)
    older = p.journals / first["transaction"] / "transaction.json"
    latest = p.journals / second["transaction"] / "transaction.json"
    with pytest.raises(ValueError, match="latest active"):
        DEPLOY["restore"](p, older, control, check=check)
    assert p.current.resolve() == third
    DEPLOY["restore"](p, latest, control, check=check)
    with pytest.raises(ValueError, match="not available"):
        DEPLOY["restore"](p, latest, control, check=check)
    assert p.current.resolve() == new
    DEPLOY["restore"](p, older, control, check=check)
    assert not p.current.exists()


def test_stage_rejects_archive_escape(tmp_path):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as bundle:
        info = tarfile.TarInfo("../escape")
        info.size = 4
        bundle.addfile(info, io.BytesIO(b"nope"))
    raw = archive.getvalue()
    p = SimpleNamespace(releases=tmp_path, mac=False)
    with pytest.raises(ValueError, match="escaping path"):
        DEPLOY["stage"](p, "a" * 40, base64.b64encode(raw), hashlib.sha256(raw).hexdigest())
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("mismatch", [None, "invocation", "group", "command", "pid"])
def test_worker_drain_checks_native_process_identity(tmp_path, mismatch):
    folder = tmp_path / "123"
    folder.mkdir()
    group = "/system.slice/cirra-ha-worker.service"
    (folder / "cgroup").write_text("0::" + ("/other" if mismatch == "group" else group))
    command = b"python\0-m\0cirra\0worker\0--state-dir\0/state\0"
    (folder / "cmdline").write_bytes(b"unrelated\0" if mismatch == "command" else command)
    status = {
        "pid": "123" if mismatch == "pid" else 123,
        "invocation_id": "stale" if mismatch == "invocation" else "current",
    }
    properties = {"InvocationID": "current", "ControlGroup": group}
    if mismatch:
        with pytest.raises(ValueError):
            DEPLOY["verify_worker_pid"](status, properties, tmp_path)
    else:
        assert DEPLOY["verify_worker_pid"](status, properties, tmp_path) == 123
