"""Stage, switch and roll back the existing Cirra HA hosts without reenrollment.

The operator pauses both workers before activation; receivers stay available.
See docs/ha-deployment.md for the ordered two-host procedure.
"""

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import platform
import re
import signal
import subprocess
import sys
import tarfile
import time
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

HOSTS = ("lugos-host", "intelmac")
SHA = re.compile(r"[0-9a-f]{40}")
DIAGNOSTICS = None


def profile(host):
    if host not in HOSTS:
        raise ValueError("target must be lugos-host or intelmac")
    mac = host == "intelmac"
    root = Path("/Users/matt/.local/share/cirra-standby" if mac else "/srv/lugos/services/cirra")
    return SimpleNamespace(
        host=host,
        mac=mac,
        root=root,
        releases=root / "releases" if mac else Path("/srv/lugos/releases/cirra"),
        current=root / "ha-current",
        journals=root / "ha-deployments",
        state=root / "state/current" if mac else Path("/srv/lugos/state/cirra-ha/current"),
        config=root / "config/ha/service.yaml" if mac else root / "ha-config/service.yaml",
        wrapper=root / "config/ha/run-candidate.py" if mac else root / "ha-launch/run-candidate.py",
        units=Path("/Users/matt/Library/LaunchAgents" if mac else "/etc/systemd/system"),
    )


def run(argv, *, timeout=60, **kwargs):
    result = subprocess.run(argv, capture_output=True, timeout=timeout, **kwargs)
    if result.returncode:
        # Subcommands may inspect protected configuration. Keep their output local.
        detail = ""
        if DIAGNOSTICS is not None:
            path = DIAGNOSTICS / ("failure-" + uuid.uuid4().hex + ".log")
            atomic_bytes(path, result.stdout + b"\n" + result.stderr)
            detail = f"; private diagnostics: {path}"
        raise RuntimeError(
            f"{Path(str(argv[0])).name} failed with exit {result.returncode}{detail}"
        )
    return result.stdout


def compatibility(p, release):
    code = """
import json,sqlite3,sys
from pathlib import Path
from cirra.store import SCHEMA_VERSION
from cirra.queue_format import check
s,r=map(Path,sys.argv[1:])
resolved=s.resolve(strict=True)
assert s.is_symlink() and resolved.parent == s.with_name(s.name+'.restores')
assert (resolved/'coordination-state.json').is_file()
with sqlite3.connect((resolved/'cirra.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
    assert db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
check(resolved,r)
print(json.dumps({'state_version':resolved.name,'schema':SCHEMA_VERSION}))
"""
    return json.loads(
        run([str(release / ".venv/bin/python"), "-I", "-B", "-c", code, str(p.state), str(release)])
    )


def deployment_paths(p):
    if p.mac:
        paths = [
            p.wrapper,
            *(
                p.units / f"foo.newman.cirra-ha-{role}.plist"
                for role in ("worker", "webhook", "donor-refresh")
            ),
            p.units / "foo.newman.cirra-private-backup.plist",
            p.root.parent / "cirra-private-backup/tools/run-coordinated-backup.py",
        ]
        health = p.units / "foo.newman.cirra-health.plist"
        return [*paths, *([health] if health.exists() else [])]
    paths = [p.wrapper]
    paths.extend(
        p.root / "ha-launch" / name
        for name in ("worker.json", "webhook.json", "worker.check.json", "webhook.check.json")
    )
    for name in (
        "cirra-health.service.d/zzz-kuma-push.conf",
        "cirra-audit-reads.service.d/95-reader-auth.conf",
        "cirra-private-backup.service.d/95-health-alert.conf",
        "cirra-private-backup@.service.d/95-health-alert.conf",
        "cirra-standby-refresh.service.d/95-health-alert.conf",
    ):
        path = p.units / name
        if path.exists():
            paths.append(path)
    paths.extend(
        p.units / name
        for name in (
            "cirra-ha-worker.service",
            "cirra-ha-webhook.service",
            "cirra-standby-refresh.service",
            "cirra-health.service.d/zz-managed-state.conf",
            "cirra-audit-reads.service.d/10-audit-counter-release.conf",
            "cirra-audit-reads.service.d/90-managed-audit-reader.conf",
            "cirra-private-backup@.service.d/80-coordinated-runtime.conf",
            "cirra-private-backup@.service.d/90-private-file-contract.conf",
        )
    )
    return paths


def planned_files(p, release):
    pattern = (
        rb"/Users/matt/\.local/share/cirra-standby/releases/[0-9a-f]{40}"
        if p.mac
        else rb"/srv/lugos/(?:releases/cirra|services/cirra/audit-read-releases)/[0-9a-f]{40}"
    )
    files = {}
    for path in deployment_paths(p):
        raw = path.read_bytes()
        changed = re.sub(pattern, str(p.current).encode(), raw)
        if p.mac:
            changed = re.sub(rb'STANDBY/"releases/[0-9a-f]{40}"', b'STANDBY/"ha-current"', changed)
        if path == p.wrapper and b"check_queue_format" not in changed:
            marker = re.search(rb"^STATE = .*\n", changed, re.M)
            if marker is None:
                raise ValueError("runtime wrapper has no known state binding")
            at = marker.end()
            changed = (
                changed[:at] + b"from cirra.queue_format import check as check_queue_format\n"
                b"check_queue_format(STATE, RELEASE)\n" + changed[at:]
            )
        if changed != raw:
            files[path] = changed
    code = """
import base64,json,sys
from cirra.pins import upgrade_files
updates=upgrade_files(sys.argv[1])
print(json.dumps({str(k):base64.b64encode(v).decode() for k,v in updates.items()}))
"""
    pins = json.loads(
        run([str(release / ".venv/bin/python"), "-I", "-B", "-c", code, str(p.config)])
    )
    files.update({Path(k): base64.b64decode(v) for k, v in pins.items()})
    return files


def atomic_bytes(path, raw, *, mode=0o600, uid=None, gid=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".next-" + uuid.uuid4().hex)
    with temporary.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    if os.name != "nt" and os.geteuid() == 0 and uid is not None:
        os.chown(temporary, uid, gid)
    os.replace(temporary, path)


def set_pointer(path, target):
    if path.exists() and not path.is_symlink():
        raise ValueError("runtime selector is not a symlink")
    temporary = path.with_name(path.name + ".next-" + uuid.uuid4().hex)
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, path)


def verify_worker_pid(status, properties, proc=Path("/proc")):
    """A heartbeat PID must belong to this unit invocation and run Cirra's worker."""
    pid = status.get("pid")
    if type(pid) is not int or pid <= 1 or not properties.get("InvocationID"):
        raise ValueError("worker has no verified live process identity")
    if status.get("invocation_id") != properties["InvocationID"]:
        raise ValueError("worker heartbeat belongs to a different service invocation")
    groups = (proc / str(pid) / "cgroup").read_text().splitlines()
    args = (proc / str(pid) / "cmdline").read_bytes().split(b"\0")
    if (
        not properties.get("ControlGroup")
        or properties["ControlGroup"] not in [row.split(":", 2)[-1] for row in groups]
        or not any(args[i : i + 3] == [b"-m", b"cirra", b"worker"] for i in range(len(args) - 2))
    ):
        raise ValueError("heartbeat PID does not identify this unit's Cirra worker")
    return pid


class Controller:
    def __init__(self, p):
        self.p = p
        self.domain = f"gui/{os.getuid()}" if p.mac else None

    def label(self, role):
        return f"foo.newman.cirra-ha-{role}"

    def active(self, role):
        if self.p.mac:
            return (
                subprocess.run(
                    ["launchctl", "print", self.domain + "/" + self.label(role)],
                    capture_output=True,
                    timeout=10,
                ).returncode
                == 0
            )
        result = run(
            [
                "systemctl",
                "show",
                "-p",
                "LoadState",
                "-p",
                "ActiveState",
                f"cirra-ha-{role}.service",
            ],
            timeout=10,
        )
        properties = dict(row.split("=", 1) for row in result.decode().splitlines())
        if properties.get("LoadState") != "loaded":
            raise ValueError("cannot establish the installed service state")
        state = properties.get("ActiveState")
        if state in ("inactive", "failed"):
            return False
        if state in ("active", "activating", "deactivating", "reloading", "maintenance"):
            return True
        raise ValueError("cannot establish whether the service is stopped")

    def stop_worker(self):
        if not self.active("worker"):
            return
        if self.p.mac:
            status = json.loads((self.p.state / "worker-status.json").read_bytes())
            if status.get("state") != "standby":
                raise ValueError("return authority to Linux before unloading the Mac worker")
            run(["launchctl", "bootout", self.domain + "/" + self.label("worker")])
        else:
            # The installed handler finishes the bounded pass. Never force-kill it.
            status = json.loads((self.p.state / "worker-status.json").read_bytes())
            pid = status.get("pid")
            if type(pid) is not int or pid <= 1:
                raise ValueError("worker heartbeat has no live process identity")
            # Pin the kernel process identity before inspecting /proc to reject PID reuse.
            descriptor = os.pidfd_open(pid)
            try:
                raw = run(
                    [
                        "systemctl",
                        "show",
                        "cirra-ha-worker.service",
                        "-p",
                        "InvocationID",
                        "-p",
                        "ControlGroup",
                    ]
                )
                properties = dict(row.split("=", 1) for row in raw.decode().splitlines())
                verify_worker_pid(status, properties)
                signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            finally:
                os.close(descriptor)
            deadline = time.monotonic() + 1200
            while self.active("worker"):
                if time.monotonic() >= deadline:
                    raise RuntimeError("worker has not drained; services and state were retained")
                time.sleep(1)

    def start_worker(self):
        if self.active("worker"):
            return
        if self.p.mac:
            run(
                [
                    "launchctl",
                    "bootstrap",
                    self.domain,
                    str(self.p.units / (self.label("worker") + ".plist")),
                ]
            )
        else:
            run(["systemctl", "start", "cirra-ha-worker.service"])

    def restart_receivers(self):
        if self.p.mac:
            for role in ("webhook",):
                if self.active(role):
                    run(["launchctl", "bootout", self.domain + "/" + self.label(role)])
                run(
                    [
                        "launchctl",
                        "bootstrap",
                        self.domain,
                        str(self.p.units / (self.label(role) + ".plist")),
                    ]
                )
        else:
            run(["systemctl", "daemon-reload"])
            run(["systemctl", "restart", "cirra-ha-webhook.service", "cirra-audit-reads.service"])

    @contextlib.contextmanager
    def maintenance_paused(self):
        """Keep scheduled readers on a single release during the short file switch."""
        paused = []
        try:
            if self.p.mac:
                for label in (
                    "foo.newman.cirra-ha-donor-refresh",
                    "foo.newman.cirra-private-backup",
                    "foo.newman.cirra-health",
                ):
                    deadline = time.monotonic() + 1500
                    while True:
                        status = subprocess.run(
                            ["launchctl", "print", self.domain + "/" + label],
                            capture_output=True,
                            timeout=10,
                        )
                        if status.returncode:
                            break
                        if b"state = running" not in status.stdout:
                            run(["launchctl", "bootout", self.domain + "/" + label])
                            paused.append(label)
                            break
                        if time.monotonic() >= deadline:
                            raise RuntimeError("scheduled maintenance has not finished")
                        time.sleep(1)
            else:
                for timer in ("cirra-private-backup", "cirra-standby-refresh", "cirra-health"):
                    if (
                        subprocess.run(
                            ["systemctl", "is-active", "--quiet", timer + ".timer"],
                            capture_output=True,
                            timeout=10,
                        ).returncode
                        == 0
                    ):
                        run(["systemctl", "stop", timer + ".timer"])
                        paused.append(timer)
                deadline = time.monotonic() + 1500
                while True:
                    output = run(
                        [
                            "systemctl",
                            "list-units",
                            "--no-legend",
                            "--plain",
                            "--state=active,activating,deactivating",
                            "cirra-private-backup*.service",
                            "cirra-standby-refresh.service",
                            "cirra-health.service",
                        ]
                    )
                    if not output.strip():
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("scheduled maintenance has not finished")
                    time.sleep(1)
            yield
        finally:
            failures = []
            for name in paused:
                try:
                    if self.p.mac:
                        run(
                            [
                                "launchctl",
                                "bootstrap",
                                self.domain,
                                str(self.p.units / (name + ".plist")),
                            ]
                        )
                    else:
                        run(["systemctl", "start", name + ".timer"])
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    failures.append(name)
            if failures:
                raise RuntimeError("scheduled maintenance did not resume: " + ", ".join(failures))

    def ready(self):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            try:
                for port in [18796] if self.p.mac else [18796, 8797]:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/healthz", timeout=3
                    ) as response:
                        if response.status != 200:
                            raise ValueError("receiver is not healthy")
                return
            except (OSError, ValueError):
                time.sleep(0.5)
        raise RuntimeError("candidate receiver readiness failed")


def record(path, document):
    atomic_bytes(path, (json.dumps(document, indent=2) + "\n").encode())


def restore(p, journal, controller, *, check=compatibility):
    document = json.loads(journal.read_bytes())
    if document["phase"] not in ("prepared", "applied", "verified"):
        raise ValueError("transaction is not available for rollback")
    live = [
        json.loads(path.read_bytes())
        for path in p.journals.glob("*/transaction.json")
        if path != journal
    ]
    if any(
        row["phase"] != "rolled_back" and row["sequence"] > document["sequence"] for row in live
    ):
        raise ValueError("roll back the latest active transaction first")
    pointer = str(p.current.resolve(strict=True)) if p.current.is_symlink() else None
    allowed = [document["release"]]
    if document["phase"] == "prepared":
        allowed.append(document["previous_release"] if document["previous_pointer"] else None)
    if pointer not in allowed:
        raise ValueError("runtime selector changed outside this deployment transaction")
    previous = Path(document["previous_release"])
    check(p, previous)
    for index, row in enumerate(document["files"]):
        path = Path(row["path"])
        if row["before"] is None:
            if path.exists():
                path.replace(journal.parent / f"retained-new-file-{index}")
        else:
            atomic_bytes(
                path,
                base64.b64decode(row["before"]),
                mode=row["mode"],
                uid=row["uid"],
                gid=row["gid"],
            )
    if document["previous_pointer"] is None:
        if p.current.is_symlink():
            p.current.replace(journal.parent / "retained-runtime-pointer")
    else:
        set_pointer(p.current, document["previous_pointer"])
    controller.restart_receivers()
    controller.ready()
    document["phase"] = "rolled_back"
    record(journal, document)


def activate(p, release, files, controller, *, peer_healthy, check=compatibility):
    if not peer_healthy:
        raise ValueError("verify the peer receiver before restarting this origin")
    if controller.active("worker"):
        raise ValueError("both workers must be paused before activation")
    if any(
        json.loads(x.read_bytes()).get("phase") not in ("verified", "rolled_back")
        for x in p.journals.glob("*/transaction.json")
    ):
        raise ValueError("reconcile the unfinished deployment transaction first")
    state = check(p, release)
    previous_pointer = os.readlink(p.current) if p.current.is_symlink() else None
    if previous_pointer is not None:
        previous = p.current.resolve(strict=True)
    else:
        match = re.search(rb"^RELEASE = Path\([\"']([^\"']+)[\"']\)", p.wrapper.read_bytes(), re.M)
        if match is None:
            raise ValueError("cannot identify the previous runtime")
        previous = Path(match[1].decode())
    check(p, previous)
    identifier = uuid.uuid4().hex
    journal = p.journals / identifier / "transaction.json"
    default = p.config.stat()
    rows = []
    for path in files:
        info = path.stat() if path.exists() else default
        rows.append(
            {
                "path": str(path),
                "before": base64.b64encode(path.read_bytes()).decode() if path.exists() else None,
                "mode": info.st_mode & 0o777,
                "uid": info.st_uid,
                "gid": info.st_gid,
            }
        )
    document = {
        "phase": "prepared",
        "sequence": 1
        + max(
            (
                json.loads(path.read_bytes())["sequence"]
                for path in p.journals.glob("*/transaction.json")
            ),
            default=0,
        ),
        "release": str(release),
        "previous_release": str(previous),
        "previous_pointer": previous_pointer,
        "state": state,
        "files": rows,
    }
    record(journal, document)
    try:
        for row in rows:
            atomic_bytes(
                Path(row["path"]),
                files[Path(row["path"])],
                mode=row["mode"],
                uid=row["uid"],
                gid=row["gid"],
            )
        set_pointer(p.current, release)
        document["phase"] = "applied"
        record(journal, document)
        controller.restart_receivers()
        controller.ready()
        document["phase"] = "verified"
        record(journal, document)
    except BaseException:
        restore(p, journal, controller, check=check)
        raise
    return {
        "host": p.host,
        "release": release.name,
        "transaction": identifier,
        "state_version": state["state_version"],
        "worker": "paused",
        "receivers": "healthy",
        "rollback_command": f"python scripts/deploy-ha.py rollback --host {p.host} "
        f"--transaction {identifier}",
    }


def stage(p, sha, encoded, expected):
    archive = base64.b64decode(encoded, validate=True)
    if len(archive) > 32 * 1024 * 1024 or hashlib.sha256(archive).hexdigest() != expected:
        raise ValueError("release archive hash or size mismatch")
    release = p.releases / sha
    marker = release / ".ha-install.json"
    if release.exists():
        if (
            not marker.is_file()
            or json.loads(marker.read_bytes()).get("archive_sha256") != expected
        ):
            raise ValueError("existing release lacks this installation receipt; retained unchanged")
        return {"release": sha, "reused": True, "archive_sha256": expected}
    release.mkdir(parents=True, mode=0o755)
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for item in bundle.getmembers():
            if (not item.isfile() and not item.isdir()) or not (
                release / item.name
            ).resolve().is_relative_to(release):
                raise ValueError("release archive contains a non-file or escaping path")
        bundle.extractall(release, filter="data")
    prefix = []
    if not p.mac:
        import pwd

        operator = pwd.getpwnam("matt")
        for path in [release, *release.rglob("*")]:
            os.chown(path, operator.pw_uid, operator.pw_gid)
        prefix = ["sudo", "-u", "matt", "-H"]
    run([*prefix, sys.executable, "-m", "venv", str(release / ".venv")], timeout=120)
    python = str(release / ".venv/bin/python")
    environment = dict(os.environ)
    binary_options = ["--only-binary=:all:"]
    if p.mac:
        # This Intel Mac's OS predates the cryptography wheel's deployment target.
        # Use the already provisioned compiler/OpenSSL and hashed source archive.
        environment.update(
            PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            OPENSSL_DIR="/usr/local/opt/openssl@3",
            CARGO_BUILD_JOBS="2",
            CARGO_HOME=str(p.root / "cargo-build"),
            PIP_CACHE_DIR=str(p.root / "wheel-cache"),
        )
        binary_options.append("--no-binary=cryptography")
    run(
        [
            *prefix,
            python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--require-hashes",
            *binary_options,
            "-r",
            str(release / "requirements/dev.txt"),
        ],
        timeout=600,
        env=environment,
    )
    run(
        [
            *prefix,
            python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-build-isolation",
            "--no-deps",
            str(release),
        ],
        timeout=180,
    )
    # Only public release code gets read access, for the isolated audit-reader mount.
    for path in [release, *release.rglob("*")]:
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644 | (path.stat().st_mode & 0o111))
    run([python, "-I", "-B", "-c", "import cirra.cli, cirra.pins"])
    record(marker, {"sha": sha, "archive_sha256": expected})
    return {"release": sha, "reused": False, "archive_sha256": expected}


def remote_action(params):
    import fcntl

    global DIAGNOSTICS
    os.umask(0o077)
    p = profile(params["host"])
    if params["action"] in ("probe", "preview"):
        return _remote_action(params)
    p.journals.mkdir(parents=True, exist_ok=True, mode=0o700)
    DIAGNOSTICS = p.journals
    with (p.journals / "operator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _remote_action(params)


def _remote_action(params):
    p = profile(params["host"])
    if (platform.system() == "Darwin") != p.mac or (not p.mac and os.geteuid() != 0):
        raise ValueError("wrong execution platform or operator")
    action = params["action"]
    controller = Controller(p)
    if action == "probe":
        controller.ready()
        return {"host": p.host, "healthy": True, "worker_active": controller.active("worker")}
    if action == "worker-stop":
        controller.stop_worker()
        return {"host": p.host, "worker": "paused"}
    if action == "worker-start":
        controller.start_worker()
        return {"host": p.host, "worker": "started"}
    if action == "rollback":
        if controller.active("worker") or not params.get("peer_healthy"):
            raise ValueError("pause workers and verify peer before rollback")
        identifier = params["transaction"]
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("invalid transaction")
        with controller.maintenance_paused():
            restore(p, p.journals / identifier / "transaction.json", controller)
        return {"host": p.host, "transaction": identifier, "state": "rolled_back"}
    sha = params.get("sha", "")
    if not SHA.fullmatch(sha):
        raise ValueError("release must be an immutable full Git SHA")
    if action == "stage":
        return stage(p, sha, params["archive"], params["archive_sha256"])
    release = p.releases / sha
    compatibility(p, release)
    files = planned_files(p, release)
    if action == "preview":
        return {
            "host": p.host,
            "release": sha,
            "files": [str(k) for k in files],
            "worker_active": controller.active("worker"),
            "state": "compatible",
        }
    if action != "activate":
        raise ValueError("unknown deployment action")
    with controller.maintenance_paused():
        return activate(
            p, release, files, controller, peer_healthy=params.get("peer_healthy", False)
        )


def dispatch(params):
    source = Path(__file__).read_text(encoding="utf-8")
    script = "__name__ = 'cirra_ha_remote'\n" + source
    script += (
        "\ntry:\n    result=remote_action(" + repr(params) + ")\n"
        "except Exception as exc:\n    result={'error':type(exc).__name__, 'detail':str(exc)}\n"
        "print(json.dumps(result))\n"
    )
    command = (
        "/Users/matt/.local/share/cirra-standby/runtime-3.13/bin/python -"
        if params["host"] == "intelmac"
        else "sudo -n python3 -"
    )
    result = json.loads(
        run(
            [
                "ssh",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=10",
                params["host"],
                command,
            ],
            input=script.encode(),
            timeout=1500,
        )
    )
    if "error" in result:
        raise RuntimeError(result["error"] + ": " + result["detail"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "stage",
            "preview",
            "activate",
            "rollback",
            "probe",
            "worker-stop",
            "worker-start",
        ),
    )
    parser.add_argument("--host", choices=HOSTS, required=True)
    parser.add_argument("--sha")
    parser.add_argument("--transaction")
    args = parser.parse_args()
    params = vars(args)
    if args.action == "stage":
        if not SHA.fullmatch(args.sha or ""):
            parser.error("stage requires a full --sha")
        archive = run(["git", "archive", "--format=tar", args.sha])
        params.update(
            archive=base64.b64encode(archive).decode(),
            archive_sha256=hashlib.sha256(archive).hexdigest(),
        )
    if args.action in ("activate", "rollback"):
        peer = next(host for host in HOSTS if host != args.host)
        evidence = dispatch({"action": "probe", "host": peer})
        if evidence["worker_active"]:
            parser.error("pause both workers before activation or rollback")
        params["peer_healthy"] = evidence["healthy"]
    print(json.dumps(dispatch(params), indent=2))


if __name__ == "__main__":
    main()
