"""A Cirra GLAS launch must not inherit ambient credential values."""

import importlib.util
import sys
from pathlib import Path


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
        "LUGOS_EVENT_AUTH_TOKEN",
    ):
        assert name not in observed["env"]


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
