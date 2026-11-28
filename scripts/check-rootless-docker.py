"""Fail closed before starting a Linux worker that requires rootless Docker."""

import argparse
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path


def check(context):
    if os.geteuid() == 0 or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", context):
        raise ValueError("rootless preflight requires a non-root account and local context")

    def run(arguments):
        # Keep this host-installed guard independent of application rollbacks.
        result = subprocess.run(
            ["docker", "--context", context, *arguments],
            env={name: os.environ[name] for name in ("PATH", "HOME") if name in os.environ},
            capture_output=True,
            check=True,
            timeout=15,
        )
        return json.loads(result.stdout)

    endpoint = run(["context", "inspect", context, "--format", "{{json .Endpoints.docker.Host}}"])
    if not endpoint.startswith("unix:///"):
        raise ValueError("rootless preflight requires a local Unix socket")
    socket = Path(endpoint.removeprefix("unix://")).stat()
    if not stat.S_ISSOCK(socket.st_mode) or socket.st_uid != os.geteuid():
        raise ValueError("rootless Docker socket must belong to the service account")
    info = run(["info", "--format", "{{json .}}"])
    if isinstance(info, dict) and not info.get("ID") and not info.get("OSType"):
        # Some Docker CLI versions return success with only client metadata
        # while the daemon is starting. Never treat that as a ready engine.
        raise ConnectionRefusedError("rootless daemon is not ready")
    if (
        not isinstance(info, dict)
        or info.get("OSType") != "linux"
        or "name=rootless" not in (info.get("SecurityOptions") or [])
        or info.get("CgroupVersion") != "2"
        or info.get("CgroupDriver") != "systemd"
        or any(
            info.get(name) is not True
            for name in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "CpuCfsPeriod", "PidsLimit")
        )
    ):
        raise ValueError("rootless Docker with enforceable cgroup v2 resource limits is required")
    return {"rootless": True, "uid": os.geteuid(), "cgroup_version": info["CgroupVersion"]}


def wait_ready(context, seconds):
    if not 0 <= seconds <= 120:
        raise ValueError("readiness wait must be between zero and 120 seconds")
    deadline = time.monotonic() + seconds
    while True:
        try:
            return check(context)
        except (FileNotFoundError, ConnectionRefusedError, subprocess.CalledProcessError):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(1, remaining))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--wait-seconds", type=int, default=30)
    args = parser.parse_args()
    try:
        print(json.dumps(wait_ready(args.context, args.wait_seconds)))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Rootless observer preflight failed ({type(error).__name__}).\n")
