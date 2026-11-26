"""A Cirra GLAS launch must not inherit ambient credential values."""

import importlib.util
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest


def load_launcher():
    script = Path(__file__).resolve().parents[1] / "scripts" / "with-glas.py"
    spec = importlib.util.spec_from_file_location("cirra_with_glas", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_discards_parent_credentials_before_glas(tmp_path, monkeypatch):
    module = load_launcher()
    script = module.__file__
    manifest = tmp_path / "config" / "glas.local.json"
    manifest.parent.mkdir()
    manifest.write_text("{}")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setenv("GLAS_MANIFEST", str(manifest))
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "untrusted-parent-access")
    monkeypatch.setenv("CIRRA_GITHUB_WEBHOOK_SECRET", "untrusted-parent-webhook")
    monkeypatch.setenv("CIRRA_GITHUB_APP_PRIVATE_KEY_PEM", "untrusted-parent-pem")
    monkeypatch.setenv("CIRRA_ROUTING_KEY", "untrusted-parent-routing")
    monkeypatch.setenv("LUGOS_EVENT_AUTH_TOKEN", "untrusted-parent-event")
    monkeypatch.setattr(sys, "argv", [str(script), "--exec", sys.executable, "-c", "pass"])
    observed = {}

    def capture(command, *, cwd, env):
        observed.update(command=command, cwd=cwd, env=env)
        return 0

    monkeypatch.setattr(module.subprocess, "call", capture)
    assert module.main() == 0
    assert observed["command"][1:5] == ["-m", "glas", "run", "--manifest"]
    for name in (
        "BWS_ACCESS_TOKEN",
        "CIRRA_GITHUB_WEBHOOK_SECRET",
        "CIRRA_GITHUB_APP_PRIVATE_KEY_PEM",
        "CIRRA_ROUTING_KEY",
        "LUGOS_EVENT_AUTH_TOKEN",
    ):
        assert name not in observed["env"]


@pytest.mark.parametrize(("role", "command"), [("worker", "worker"), ("webhook", "serve-webhooks")])
def test_macos_jobs_launch_with_host_local_arguments_and_separate_manifests(role, command):
    script = Path(__file__).resolve().parents[1] / "scripts/macos-service-plist.py"
    expected = {
        "home": "/Users/matt",
        "release": "/private/Cirra release/current",
        "python": "/private/py",
        "service-config": "/private/config/service.yaml",
        "state": "/private/data-current",
        "manifest": f"/private/config/{role}.json",
        "glas-root": "/private/glas",
        "glas-home": f"/private/glas-{role}",
        "logs": "/private/logs",
    }
    flags = [field for name, path in expected.items() for field in ("--" + name, path)]
    job = plistlib.loads(subprocess.check_output([sys.executable, str(script), role, *flags]))
    assert job["ProgramArguments"][:3] == [
        expected["python"],
        expected["release"] + "/scripts/with-glas.py",
        command,
    ]
    assert job["ProgramArguments"][3:7] == [
        "--service-config",
        expected["service-config"],
        "--state-dir",
        expected["state"],
    ]
    assert job["EnvironmentVariables"]["GLAS_MANIFEST"] == expected["manifest"]
    assert job["EnvironmentVariables"]["GLAS_HOME"] == expected["glas-home"]
    assert job["LimitLoadToSessionType"] == "Aqua" and job["Umask"] == 0o077


def test_manifest_is_selected_relative_to_the_callers_directory(tmp_path, monkeypatch):
    module = load_launcher()
    repo = tmp_path / "repo"
    caller = tmp_path / "caller"
    repo.mkdir()
    caller.mkdir()
    filename = "glas.local.json"
    (caller / filename).write_text("selected by caller")
    (repo / filename).write_text("different repository manifest")
    monkeypatch.chdir(caller)
    monkeypatch.setattr(module, "ROOT", repo)
    monkeypatch.setenv("GLAS_MANIFEST", filename)
    monkeypatch.setattr(sys, "argv", [module.__file__, "--exec", "unused"])

    def check_manifest(command, *, cwd, env):
        selected = Path(command[command.index("--manifest") + 1])
        assert cwd == repo
        assert (cwd / selected).read_text() == "selected by caller"
        return 0

    monkeypatch.setattr(module.subprocess, "call", check_manifest)
    assert module.main() == 0


@pytest.mark.parametrize("interpreter", ["tools/python", "python"])
def test_relative_glas_settings_use_the_callers_directory(tmp_path, monkeypatch, interpreter):
    module = load_launcher()
    caller = tmp_path / "caller"
    repo = tmp_path / "repo"
    checkout = caller / "glas"
    (checkout / "src/glas").mkdir(parents=True)
    (checkout / "src/glas/cli.py").touch()
    repo.mkdir()
    manifest = caller / "manifest.json"
    manifest.write_text("{}")
    monkeypatch.chdir(caller)
    monkeypatch.setattr(module, "ROOT", repo)
    monkeypatch.setattr(sys, "argv", [module.__file__, "--exec", "unused"])
    monkeypatch.setenv("GLAS_MANIFEST", str(manifest))
    monkeypatch.setenv("GLAS_ROOT", "glas")
    monkeypatch.setenv("GLAS_HOME", "state")
    monkeypatch.setenv("GLAS_PYTHON", interpreter)

    def check_paths(command, *, cwd, env):
        assert cwd == repo
        expected_interpreter = (
            caller / interpreter if interpreter == "tools/python" else Path("python")
        )
        assert Path(command[0]) == expected_interpreter
        assert Path(env["GLAS_HOME"]) == caller / "state"
        assert str(checkout / "src") in env["PYTHONPATH"].split(module.os.pathsep)
        return 0

    monkeypatch.setattr(module.subprocess, "call", check_paths)
    assert module.main() == 0
