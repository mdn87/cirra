"""Launch this application's trusted command with explicit BWS references."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ["{python}", "-m", "cirra"]


def main():
    manifest = ROOT / "config/glas.local.json"
    candidates = (
        [Path(os.environ["GLAS_ROOT"])]
        if os.environ.get("GLAS_ROOT")
        else [p / n for p in ROOT.parents for n in ("lugos/glas", "glas")]
    )
    glas = next((p for p in candidates if (p / "src/glas/cli.py").is_file()), None)
    if glas is None or not manifest.is_file():
        print("Glas checkout or local reference manifest is missing.", file=sys.stderr)
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
    env["GLAS_HOME"] = str(glas / ".glas")
    if (ROOT / "src").is_dir():
        env["PYTHONPATH"] = str(ROOT / "src") + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
    command = [c[:-4] if os.name != "nt" and c in ("npm.cmd", "pnpm.cmd") else c for c in command]
    return subprocess.call(
        [
            str(glas / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")),
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
