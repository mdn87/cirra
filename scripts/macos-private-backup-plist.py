"""Render the Intel Mac's private-state backup LaunchAgent using credential file references."""

import argparse
import plistlib
import re
import sys
from pathlib import PurePosixPath


def absolute(value):
    if not PurePosixPath(value).is_absolute():
        raise argparse.ArgumentTypeError("use an absolute Mac-local path")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("python", "state-dir", "repository-env", "approval-file", "witness-file", "logs"):
        parser.add_argument("--" + name, type=absolute, required=True)
    parser.add_argument("--expected-host", required=True)
    parser.add_argument("--witness-name", default="CIRRA_WITNESS_KEY")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{64}", args.expected_host):
        parser.error("expected-host must identify the local authorized data host")
    cmd = [args.python, "-m", "cirra.private_transfer", "backup"]
    for name in (
        "state-dir",
        "repository-env",
        "approval-file",
        "witness-file",
        "witness-name",
        "expected-host",
    ):
        cmd.extend(("--" + name, getattr(args, name.replace("-", "_"))))
    plistlib.dump(
        {
            "Label": "foo.newman.cirra-private-backup",
            "ProgramArguments": cmd,
            "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"},
            "RunAtLoad": True,
            "StartInterval": 900,
            "LimitLoadToSessionType": "Aqua",
            "Umask": 0o077,
            "StandardOutPath": str(PurePosixPath(args.logs) / "private-backup.log"),
            "StandardErrorPath": str(PurePosixPath(args.logs) / "private-backup.log"),
        },
        sys.stdout.buffer,
    )


if __name__ == "__main__":
    main()
