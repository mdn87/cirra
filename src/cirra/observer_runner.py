"""Trusted container launcher, mounted read-only by the host; never imported from a PR.

This script runs INSIDE the credential-free sandbox. The host observes Docker's
exit status; output and any files created here cannot manufacture a witness.
"""

import json
import os
import pathlib
import subprocess
import tarfile


def main():
    root = pathlib.Path("/work/source")
    root.mkdir()
    with tarfile.open("/input/source.tar") as archive:
        for member in archive:
            parts = pathlib.PurePosixPath(member.name).parts
            if not member.isfile() or not parts or member.name.startswith("/") or ".." in parts:
                raise ValueError("invalid source archive")
            target = root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open("xb") as output:
                while chunk := source.read(65536):
                    output.write(chunk)
            target.chmod(member.mode & 0o777)
    commands = json.loads(pathlib.Path("/input/commands.json").read_text())
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(root / "src"),
        "PYTHONUNBUFFERED": "1",
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    os.chdir(root)
    for command in commands:
        result = subprocess.run(command, env=env, check=False)
        if result.returncode:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
