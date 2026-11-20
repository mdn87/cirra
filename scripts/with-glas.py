"""Launch this application's trusted command with explicit BWS references."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ["{python}", "-m", "cirra"]


def main():
    manifest = Path(os.environ.get("GLAS_MANIFEST", ROOT / "config/glas.local.json"))
    candidates = (
        [Path(os.environ["GLAS_ROOT"])]
        if os.environ.get("GLAS_ROOT")
        else [p / n for p in ROOT.parents for n in ("lugos/glas", "glas")]
    )
    glas = next((p for p in candidates if (p / "src/glas/cli.py").is_file()), None)
    if not manifest.is_file():
        print("Glas reference manifest is missing.", file=sys.stderr)
        return 1
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    app_python = str(python) if python.is_file() else sys.executable
    args = sys.argv[1:]
    command = (
        args[1:]
        if args[:1] == ["--exec"]
        else [app_python if x == "{python}" else x for x in DEFAULT] + args
    )
    if not command:
        print("Supply a trusted command after --exec.", file=sys.stderr)
        return 2
    env = os.environ.copy()
    for name in tuple(env):
        upper = name.upper()
        if upper == "BWS_ACCESS_TOKEN" or upper.endswith(
            ("_TOKEN", "_SECRET", "_PASSWORD", "_KEY", "_PEM")
        ):
            env.pop(name, None)
    if "GLAS_HOME" not in env and glas is not None:
        env["GLAS_HOME"] = str(glas / ".glas")
    sources = [str(ROOT / "src")]
    if glas is not None:
        sources.append(str(glas / "src"))
    if env.get("PYTHONPATH"):
        sources.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(sources)
    command = [c[:-4] if os.name != "nt" and c in ("npm.cmd", "pnpm.cmd") else c for c in command]
    glas_python = os.environ.get("GLAS_PYTHON")
    if not glas_python:
        glas_python = (
            str(glas / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
            if glas is not None
            else sys.executable
        )
    return subprocess.call(
        [
            glas_python,
            "-m",
            "glas",
            "run",
            "--manifest",
            str(manifest),
            "--",
            *command,
        ],
        cwd=ROOT,
        env=env,
    )


if __name__ == "__main__":
    raise SystemExit(main())
