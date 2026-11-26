"""Render a per-user launchd job for one Cirra service with its own Glas manifest."""

import argparse
import plistlib
import sys
from pathlib import Path, PurePosixPath


def absolute(value):
    path = PurePosixPath(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("use an absolute host-local path")
    return str(path)


def main(argv=None):
    args = argparse.ArgumentParser(description=__doc__)
    commands = {
        "worker": "worker",
        "webhook": "serve-webhooks",
        "standby-refresh": "refresh-standby-snapshot",
    }
    args.add_argument("role", choices=commands)
    args.add_argument("--home", default=str(Path.home()), type=absolute)
    for name in (
        "release",
        "python",
        "service-config",
        "state",
        "manifest",
        "glas-root",
        "glas-home",
        "logs",
    ):
        args.add_argument("--" + name, required=True, type=absolute)
    paths = args.parse_args(argv)
    role = paths.role
    command = [
        paths.python,
        str(PurePosixPath(paths.release) / "scripts" / "with-glas.py"),
        commands[role],
        "--service-config",
        paths.service_config,
        "--state-dir",
        paths.state,
    ]
    if role == "webhook":
        command.extend(("--port", "8796"))
    plist = {
        "Label": "foo.newman.cirra-" + role,
        "ProgramArguments": command,
        "WorkingDirectory": paths.release,
        "EnvironmentVariables": {
            "GLAS_ROOT": paths.glas_root,
            "GLAS_HOME": paths.glas_home,
            "GLAS_MANIFEST": paths.manifest,
            "PATH": ":".join(
                (
                    str(PurePosixPath(paths.home) / ".local/bin"),
                    "/usr/local/bin",
                    "/opt/homebrew/bin",
                    "/usr/bin",
                    "/bin",
                    "/usr/sbin",
                    "/sbin",
                )
            ),
        },
        "LimitLoadToSessionType": "Aqua",
        "Umask": 0o077,
        "StandardOutPath": str(PurePosixPath(paths.logs) / (role + ".log")),
        "StandardErrorPath": str(PurePosixPath(paths.logs) / (role + ".log")),
    }
    if role == "standby-refresh":
        plist.update(RunAtLoad=True, StartInterval=3600)
    else:
        plist.update(KeepAlive=True, ThrottleInterval=20)
    plistlib.dump(plist, sys.stdout.buffer)


if __name__ == "__main__":
    main()
