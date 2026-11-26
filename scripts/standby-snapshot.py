"""Refresh the passive Cirra standby from the primary's last verified backup."""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

RECEIPT = Path("/var/lib/cirra-recovery/last-verified.json")
STATE = "/srv/lugos/state/cirra"
ID = re.compile(r"[0-9a-f]{64}")
MAX_AGE = timedelta(days=1)
RESERVE = 2 * 1024**3


def check_receipt(record):
    if not isinstance(record, dict) or not ID.fullmatch(str(record.get("snapshot_id"))):
        raise ValueError("invalid standby snapshot identity")
    if record.get("sqlite_integrity") != "ok" or record.get("foreign_key_errors") != 0:
        raise ValueError("source restore did not pass database verification")
    for key, minimum in (
        ("schema_version", 1),
        ("current_witnesses", 0),
        ("historical_witnesses", 0),
    ):
        if type(record.get(key)) is not int or record[key] < minimum:
            raise ValueError("missing restore evidence")
    captured, verified = (
        datetime.fromisoformat(str(record.get(k))) for k in ("captured_at", "verified_at")
    )
    if captured.utcoffset() is None or verified.utcoffset() is None or verified < captured:
        raise ValueError("invalid verification times")
    if verified > datetime.now(UTC) + timedelta(minutes=5):
        raise ValueError("verification time is in the future")
    return captured


def require_fresh(record):
    if datetime.now(UTC) - check_receipt(record) > MAX_AGE:
        raise ValueError("standby recovery point exceeds 24 hours")


def export(receipt_path, request, output):
    repository = os.environ.get("CIRRA_APPROVED_BACKUP_REPOSITORY")
    if not repository or repository != os.environ.get("RESTIC_REPOSITORY"):
        raise ValueError("standby source repository differs from the approved backup repository")
    previous = request.readline(66)
    if not re.fullmatch(rb"(?:-|[0-9a-f]{64})\n", previous):
        raise ValueError("invalid standby request")
    record = json.loads(receipt_path.read_bytes())
    require_fresh(record)
    snapshot = record["snapshot_id"]
    if previous[:-1] == snapshot.encode("ascii"):
        return
    # Neither the requested value nor SSH_ORIGINAL_COMMAND selects the object or path.
    args = [
        "restic",
        "--no-cache",
        "--retry-lock",
        "15s",
        "dump",
        "--archive",
        "tar",
        snapshot,
        STATE,
    ]
    with zipfile.ZipFile(output, "w") as bundle:
        with bundle.open("state.tar", "w", force_zip64=True) as dest:
            with subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            ) as source:
                try:
                    shutil.copyfileobj(source.stdout, dest, 1024 * 1024)
                finally:
                    source.stdout.close()
                if source.wait():
                    raise OSError("verified snapshot export failed")
        bundle.writestr("verified.json", json.dumps(record))


def read_bundle(path):
    with zipfile.ZipFile(path) as bundle:
        if sorted(bundle.namelist()) != ["state.tar", "verified.json"]:
            raise ValueError("unexpected standby archive entries")
        if any(i.compress_type != zipfile.ZIP_STORED or i.flag_bits & 1 for i in bundle.infolist()):
            raise ValueError("unexpected standby archive encoding")
        if not 0 < bundle.getinfo("verified.json").file_size <= 4096:
            raise ValueError("invalid backup receipt size")
        if not bundle.getinfo("state.tar").file_size or bundle.testzip() is not None:
            raise ValueError("standby archive checksum failed")
        receipt = json.loads(bundle.read("verified.json"))
    check_receipt(receipt)
    return receipt


def refresh(root, command):
    import fcntl

    info = root.lstat()
    if (
        root.resolve() != root
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("standby directory must be local, private and owned by this account")
    current = root / "current.zip"
    # The lock also covers manual refreshes racing with the scheduled job.
    with (root / "refresh.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            previous = read_bundle(current)
        except (FileNotFoundError, ValueError, zipfile.BadZipFile):
            previous = None
        known = previous["snapshot_id"] if previous else "-"
        # Fixed staging path bounds storage even after a hard reboot during a download.
        stage = root / "refresh.incoming"
        with private_stage(stage) as target:
            available = shutil.disk_usage(root).free - RESERVE
            with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as source:
                try:
                    source.stdin.write((known + "\n").encode("ascii"))
                    source.stdin.close()
                    while data := source.stdout.read(1024 * 1024):
                        if target.tell() + len(data) > available:
                            raise OSError("insufficient disk space for standby refresh")
                        target.write(data)
                    if source.wait():
                        raise OSError("standby source did not complete the transfer")
                finally:
                    if source.poll() is None:
                        source.kill()
            if target.tell() == 0 and previous is not None:
                require_fresh(previous)
                return previous
            target.flush()
            os.fsync(target.fileno())
        record = read_bundle(stage)
        require_fresh(record)
        if previous and check_receipt(record) < check_receipt(previous):
            raise ValueError("source offered an older recovery point")
        os.replace(stage, current)
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return record


def private_stage(path):
    fd = os.open(path, os.O_CREAT | os.O_NOFOLLOW | os.O_TRUNC | os.O_RDWR, 0o600)
    return os.fdopen(fd, "w+b")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "refresh", "status"))
    parser.add_argument("--root", type=Path, default=Path.home() / ".local/share/cirra-standby")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == "export":
            export(RECEIPT, sys.stdin.buffer, sys.stdout.buffer)
            return 0
        if args.action == "refresh":
            command = [
                "/usr/bin/ssh",
                "-T",
                "-F",
                str(args.root / "source.ssh"),
                "cirra-standby-source",
            ]
            record = refresh(args.root, command)
        else:
            record = read_bundle(args.root / "current.zip")
            require_fresh(record)
        print(
            json.dumps(
                {
                    "snapshot_id": record["snapshot_id"],
                    "captured_at": record["captured_at"],
                    "current_witnesses": record["current_witnesses"],
                    "historical_witnesses": record["historical_witnesses"],
                }
            )
        )
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        print(f"Standby {args.action} failed ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
