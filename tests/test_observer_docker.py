"""Opt-in integration checks against a real local Linux Docker engine."""

import io
import os
import tarfile
import uuid

import pytest
import yaml

from cirra.domain import CirraError
from cirra.observer import DockerExecutor, load_observer

pytestmark = pytest.mark.skipif(
    not os.environ.get("CIRRA_DOCKER_TEST_IMAGE"),
    reason="set CIRRA_DOCKER_TEST_IMAGE to an operator-built immutable Linux image ID",
)


@pytest.mark.parametrize("case", ["isolated_success", "failure", "timeout"])
def test_real_container_isolation_and_cleanup(case, tmp_path, monkeypatch):
    scripts = {
        "isolated_success": """
import os, pathlib, socket
assert os.getuid() == 65534
assert 'CIRRA_WITNESS_KEY' not in os.environ
assert 'GITHUB_TOKEN' not in os.environ
assert not pathlib.Path('/var/run/docker.sock').exists()
assert sorted(p.name for p in pathlib.Path('/input').iterdir()) == [
    'commands.json', 'runner.py', 'source.tar'
]
try:
    pathlib.Path('/input/stolen').write_text('must fail')
except OSError:
    pass
else:
    raise AssertionError('input mount is writable')
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('sandbox has outbound network access')
print('Isolation assertions passed')
""",
        "failure": "raise SystemExit(7)\n",
        "timeout": "import time; time.sleep(120)\n",
    }
    raw = scripts[case].encode()
    archive = tmp_path / "source.tar"
    with tarfile.open(archive, "w") as tar:
        item = tarfile.TarInfo("check.py")
        item.size = len(raw)
        tar.addfile(item, io.BytesIO(raw))
    path = tmp_path / "policy.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "image": os.environ["CIRRA_DOCKER_TEST_IMAGE"],
                "commands": [["python", "check.py"]],
                "timeout_seconds": 1 if case == "timeout" else 30,
            }
        )
    )
    monkeypatch.setenv("CIRRA_WITNESS_KEY", "must-never-enter-the-container")
    monkeypatch.setenv("GITHUB_TOKEN", "must-never-enter-the-container")
    policy = load_observer(path)
    name = "cirra-observer-" + uuid.uuid4().hex
    executor = DockerExecutor()
    previous_umask = os.umask(0o077)
    try:
        if case == "timeout":
            with pytest.raises(CirraError, match="deadline"):
                executor.run(policy, archive, tmp_path, name)
        else:
            result = executor.run(policy, archive, tmp_path, name)
            assert result["exit_code"] == (0 if case == "isolated_success" else 1)
    finally:
        os.umask(previous_umask)
    run = executor.command(policy, tmp_path)
    assert run(["ps", "--all", "--filter", "name=^/" + name + "$", "--format", "{{.Names}}"]) == b""
