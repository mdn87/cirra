"""Back up one Cirra state directory with restic and verify an isolated restore."""

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

TAG = "cirra-state"
UNITS = (
    "cirra-health.timer",
    "cirra-webhook.service",
    "cirra-audit-reads.service",
    "cirra-worker.service",
)
HEALTH_JOBS = ("cirra-health.service", "cirra-health-alert.service", "cirra-health-resolve.service")
WITNESS = re.compile(r"[0-9a-f]{64}-(?:execution-)?witness")
DIGEST = re.compile(r"[0-9a-f]{64}")


class RecoveryError(Exception):
    """An operational error safe to send to the journal."""


def private_dir(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise RecoveryError(
            "recovery scratch and control directories must be private and owned by the operator"
        )


def command(argv, timeout=30):
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise RecoveryError(f"{Path(argv[0]).name} timed out") from None
    if result.returncode:
        raise RecoveryError(f"{Path(argv[0]).name} exited {result.returncode}")
    return result.stdout


def restic(args, *, timeout=30):
    return command(["restic", "--no-cache", "--json", "--retry-lock", "5s", *args], timeout)


def unit_state(unit):
    return (
        command(["systemctl", "show", "--property=ActiveState", "--value", unit]).decode().strip()
    )


def saved(path, value):
    temporary = path.with_suffix(".next")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def resume(journal):
    if not journal.exists():
        return
    units = json.loads(journal.read_bytes())
    if not isinstance(units, list) or any(unit not in UNITS for unit in units):
        raise RecoveryError("invalid recovery journal")
    failed = []
    for unit in reversed(units):
        try:
            command(["systemctl", "start", unit], timeout=90)
            if unit_state(unit) != "active":
                failed.append(unit)
        except RecoveryError:
            failed.append(unit)
    saved(journal, list(reversed(failed)))
    if failed:
        raise RecoveryError("could not resume " + ", ".join(failed))


@contextmanager
def exclusive(path, *, shared=False):
    import fcntl

    with path.open("rb" if shared else "a+b") as stream:
        try:
            fcntl.flock(stream, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RecoveryError("another deployment or recovery operation holds the lock") from None
        yield


def require_idle(state):
    for queue in ("queue", "audit-queue"):
        for entry in (state / queue).glob("*.json"):
            if json.loads(entry.read_bytes()).get("state") == "running":
                raise RecoveryError("the worker has a running job; retry when idle")


@contextmanager
def quiesce(state, journal):
    require_idle(state)
    states = {unit: unit_state(unit) for unit in UNITS}
    if any(value not in ("active", "inactive", "failed") for value in states.values()):
        raise RecoveryError("a Cirra service is changing state")
    active = [unit for unit in UNITS if states[unit] == "active"]
    saved(journal, active)
    try:
        for unit in active:
            command(["systemctl", "stop", unit], timeout=90)
        if any(unit_state(unit) != "inactive" for unit in active):
            raise RecoveryError("a Cirra service is still active")
        deadline = time.monotonic() + 15
        while any(
            unit_state(unit) in ("active", "activating", "deactivating") for unit in HEALTH_JOBS
        ):
            if time.monotonic() >= deadline:
                raise RecoveryError("a health notification is still active")
            time.sleep(0.25)
        require_idle(state)
        yield
    finally:
        resume(journal)


def credentials(path, name):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
        raise RecoveryError("the separate witness credential file must be a private regular file")
    matches = []
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == name:
            value = value.strip()
            if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
                value = value[1:-1]
            matches.append(value.encode())
    if len(matches) != 1 or len(matches[0]) < 32:
        raise RecoveryError("the selected witness key is missing, ambiguous or too short")
    return matches[0]


def canonical(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def file_data(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise RecoveryError("unexpected witness file type or size")
        return stream.read(1024 * 1024 + 1)


def verify_witness(path, key, *, historical):
    record = json.loads(file_data(path))
    if not isinstance(record, dict) or set(record) != {"statement", "hmac_sha256"}:
        raise RecoveryError("invalid witness record")
    expected = hmac.new(key, canonical(record["statement"]), hashlib.sha256).hexdigest()
    if not isinstance(record["hmac_sha256"], str) or not hmac.compare_digest(
        expected, record["hmac_sha256"]
    ):
        raise RecoveryError("a restored witness signature does not match the separate key")
    if historical and path.stem != hashlib.sha256(canonical(record)).hexdigest():
        raise RecoveryError("a historical witness filename does not match its contents")


def verify_state(state, key):
    db = state / "cirra.sqlite3"
    observer = state / "observer"
    history = observer / "history"
    for path in (state, observer, history):
        if not stat.S_ISDIR(path.lstat().st_mode) or path.resolve() != path:
            raise RecoveryError("unexpected restored state or observer directory")
    if not stat.S_ISREG(db.lstat().st_mode) or any(
        Path(str(db) + suffix).exists() for suffix in ("-journal", "-wal")
    ):
        raise RecoveryError("restored database must be regular and checkpointed before backup")
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RecoveryError("restored SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RecoveryError("restored SQLite foreign-key check failed")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"claims", "attempts", "reports", "audits"}.issubset(tables) or version < 3:
            raise RecoveryError("restored SQLite file does not contain the expected Cirra store")
    live_count = 0
    history_count = 0
    for path in observer.glob("*-witness.json"):
        if not WITNESS.fullmatch(path.stem):
            raise RecoveryError("unexpected observer witness filename")
        verify_witness(path, key, historical=False)
        live_count += 1
    for folder in history.glob("*-witness"):
        if not WITNESS.fullmatch(folder.name) or not stat.S_ISDIR(folder.lstat().st_mode):
            raise RecoveryError("unexpected observer history directory")
        for path in folder.glob("*.json"):
            verify_witness(path, key, historical=True)
            history_count += 1
    if not live_count or not history_count:
        raise RecoveryError("restored state contains no current or historical witness evidence")
    return {
        "schema_version": version,
        "sqlite_integrity": "ok",
        "foreign_key_errors": 0,
        "current_witnesses": live_count,
        "historical_witnesses": history_count,
    }


def check_target(args):
    if not args.repository or os.environ.get("RESTIC_REPOSITORY") != args.repository:
        raise RecoveryError("restic destination does not match the explicitly approved repository")
    private_dir(args.admin)
    private_dir(args.scratch)
    if args.state != args.state.resolve() or args.state == Path("/"):
        raise RecoveryError("the Cirra state path must be an absolute service directory")
    if args.action != "verify" and not (args.state / "cirra.sqlite3").is_file():
        raise RecoveryError("the source Cirra database does not exist")
    if any(
        path == args.state or path.is_relative_to(args.state)
        for path in (args.admin.resolve(), args.scratch.resolve(), args.witness_file.resolve())
    ):
        raise RecoveryError(
            "credentials, recovery control files and restore scratch must be outside live state"
        )
    restic(["snapshots", "--tag", TAG, "--latest", "1"])
    credentials(args.witness_file, args.witness_name)


def backup(args):
    journal = args.admin / "resume.json"
    resume(journal)
    check_target(args)
    with exclusive(args.deploy_lock, shared=True):
        started = time.monotonic()
        with quiesce(args.state, journal):
            captured = datetime.now(UTC)
            excluded = [str(args.state / subdir) for subdir in ("acceptance", "operator-recovery")]
            options = [word for path in excluded for word in ("--exclude", path)]
            out = restic(
                ["backup", "--tag", TAG, "--one-file-system", *options, "--", str(args.state)],
                timeout=args.backup_timeout,
            )
            snapshots = [
                event.get("snapshot_id")
                for line in out.splitlines()
                if (event := json.loads(line)).get("message_type") == "summary"
            ]
            if len(snapshots) != 1 or not DIGEST.fullmatch(snapshots[0] or ""):
                raise RecoveryError("restic did not acknowledge one complete snapshot")
        pause = round(time.monotonic() - started, 2)
    result = {
        "snapshot_id": snapshots[0],
        "captured_at": captured.isoformat(),
        "pause_seconds": pause,
    }
    saved(args.admin / "last-backup.json", result)
    print(json.dumps({"services_resumed": True, **result}), flush=True)
    verify(args, result)


def verify(args, captured):
    snapshot = captured["snapshot_id"]
    if not isinstance(snapshot, str) or not DIGEST.fullmatch(snapshot):
        raise RecoveryError("an exact snapshot ID is required")
    begin = time.monotonic()
    selected = json.loads(restic(["snapshots", snapshot]))
    if (
        len(selected) != 1
        or selected[0].get("id") != snapshot
        or TAG not in selected[0].get("tags", [])
        or selected[0].get("paths") != [str(args.state)]
    ):
        raise RecoveryError("the snapshot does not match the approved Cirra path and tag")
    # A replacement host recovers the capture time from authenticated restic metadata.
    captured = {"captured_at": selected[0]["time"], **captured}
    restored = Path(tempfile.mkdtemp(prefix="check-", dir=args.scratch))
    restic(
        ["restore", snapshot, "--verify", "--target", str(restored)], timeout=args.restore_timeout
    )
    checks = verify_state(
        restored / args.state.relative_to("/"), credentials(args.witness_file, args.witness_name)
    )
    verified = datetime.now(UTC)
    result = {
        **captured,
        **checks,
        "verified_at": verified.isoformat(),
        "restore_verify_seconds": round(time.monotonic() - begin, 2),
        "restore_point_age_seconds": round(
            (verified - datetime.fromisoformat(captured["captured_at"])).total_seconds(), 2
        ),
    }
    saved(args.admin / ("last-drill.json" if args.snapshot else "last-verified.json"), result)
    print(json.dumps(result, sort_keys=True), flush=True)
    if args.snapshot:
        print(f"Verified restored state in {restored / args.state.relative_to('/')}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "backup", "verify", "resume"))
    parser.add_argument("--state", type=Path, default=Path("/srv/lugos/state/cirra"))
    parser.add_argument("--admin", type=Path, default=Path("/var/lib/cirra-recovery"))
    parser.add_argument("--scratch", type=Path, default=Path("/run/cirra-recovery-restore"))
    parser.add_argument(
        "--deploy-lock", type=Path, default=Path("/srv/lugos/services/cirra/staging/deploy.lock")
    )
    parser.add_argument(
        "--witness-file", type=Path, default=Path("/srv/lugos/services/cirra/config/worker.env")
    )
    parser.add_argument("--witness-name", default="CIRRA_WITNESS_KEY")
    parser.add_argument("--snapshot", help="exact restic snapshot to verify on a replacement host")
    parser.add_argument("--repository", default=os.environ.get("CIRRA_APPROVED_BACKUP_REPOSITORY"))
    parser.add_argument("--backup-timeout", type=int, default=120)
    parser.add_argument("--restore-timeout", type=int, default=600)
    args = parser.parse_args(argv)
    if args.snapshot and args.action != "verify":
        parser.error("--snapshot is for the verify action")
    os.umask(0o077)
    try:
        private_dir(args.admin)
        with exclusive(args.admin / "operation.lock"):
            if args.action == "resume":
                resume(args.admin / "resume.json")
            elif args.action == "backup":
                backup(args)
            else:
                check_target(args)
                if args.action == "verify":
                    capture = (
                        {"snapshot_id": args.snapshot}
                        if args.snapshot
                        else json.loads((args.admin / "last-backup.json").read_bytes())
                    )
                    verify(args, capture)
                else:
                    require_idle(args.state)
                    print("Cirra backup preflight passed")
    except RecoveryError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as exc:
        # Paths, archive metadata, and secret-bearing inputs do not belong in the journal.
        print(f"Cirra recovery failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
