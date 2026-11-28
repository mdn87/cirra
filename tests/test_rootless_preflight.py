import json
import os
import runpy
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux service startup guard")
GUARD = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/check-rootless-docker.py")
)


@pytest.fixture
def engine(monkeypatch):
    temporary = tempfile.TemporaryDirectory(prefix="cirra-preflight-", dir="/tmp")
    endpoint = Path(temporary.name) / "docker.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(endpoint))
    info = {
        "OSType": "linux",
        "SecurityOptions": ["name=rootless"],
        "CgroupVersion": "2",
        "CgroupDriver": "systemd",
        **dict.fromkeys(
            ["MemoryLimit", "SwapLimit", "CpuCfsQuota", "CpuCfsPeriod", "PidsLimit"], True
        ),
    }
    calls = []

    def invoke(args, **kwargs):
        assert args[:3] == ["docker", "--context", "cirra-ha-observer"]
        assert set(kwargs["env"]) <= {"PATH", "HOME"}
        assert kwargs["timeout"] == 15 and kwargs["check"] is True
        calls.append(args[3])
        result = "unix://" + str(endpoint) if args[3] == "context" else info
        return SimpleNamespace(stdout=json.dumps(result).encode())

    monkeypatch.setenv("CIRRA_WITNESS_KEY", "must-not-reach-docker")
    monkeypatch.setattr("subprocess.run", invoke)
    try:
        yield info, calls
    finally:
        listener.close()
        temporary.cleanup()


def test_preflight_accepts_owned_rootless_socket_and_filters_credentials(engine):
    assert GUARD["check"]("cirra-ha-observer") == {
        "rootless": True,
        "uid": os.geteuid(),
        "cgroup_version": "2",
    }
    assert engine[1] == ["context", "info"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("SecurityOptions", []),
        ("CgroupVersion", "1"),
        ("CgroupDriver", "cgroupfs"),
        ("MemoryLimit", False),
        ("SwapLimit", False),
        ("CpuCfsQuota", False),
        ("CpuCfsPeriod", False),
        ("PidsLimit", None),
    ],
)
def test_preflight_refuses_unisolated_or_unbounded_engine(engine, field, value):
    engine[0][field] = value
    with pytest.raises(ValueError, match="enforceable cgroup"):
        GUARD["check"]("cirra-ha-observer")


def test_preflight_rejects_other_users_socket_before_querying_engine(engine, monkeypatch):
    uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: uid + 1)
    with pytest.raises(ValueError, match="belong to the service account"):
        GUARD["check"]("cirra-ha-observer")
    assert engine[1] == ["context"]


def test_readiness_wait_retries_missing_socket_but_not_invalid_engine(monkeypatch):
    scope = GUARD["wait_ready"].__globals__
    times, attempts = [0.0], []
    monkeypatch.setattr(scope["time"], "monotonic", lambda: times[0])
    monkeypatch.setattr(
        scope["time"], "sleep", lambda seconds: times.__setitem__(0, times[0] + seconds)
    )

    def delayed(context):
        attempts.append(context)
        if len(attempts) == 1:
            raise FileNotFoundError("daemon starting")
        return {"rootless": True}

    monkeypatch.setitem(scope, "check", delayed)
    assert GUARD["wait_ready"]("test", 2) == {"rootless": True}
    assert len(attempts) == 2 and times[0] == 1

    def wrong(context):
        raise ValueError("rootful engine")

    monkeypatch.setitem(scope, "check", wrong)
    with pytest.raises(ValueError, match="rootful"):
        GUARD["wait_ready"]("test", 2)
    assert times[0] == 1
