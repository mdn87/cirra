"""Check scheduled backup evidence without exposing the root-only recovery receipt."""

import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

CONTROL = Path("/var/lib/cirra-recovery")
MAX_CAPTURE_AGE = timedelta(hours=26)


def backup_unit_failed():
    result = subprocess.run(
        [
            "/usr/bin/systemctl",
            "show",
            "cirra-state-backup.service",
            "-p",
            "LoadState",
            "-p",
            "Result",
        ],
        capture_output=True,
        timeout=5,
        check=True,
        text=True,
    )
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines())
    return properties.get("LoadState") != "loaded" or properties.get("Result") != "success"


def problem(control, now):
    try:
        path = control / "last-verified.json"
        if not stat.S_ISREG(path.lstat().st_mode):
            return "cannot validate backup health evidence"
        with path.open("rb") as receipt:
            raw = receipt.read(8193)
    except FileNotFoundError:
        return "missing verified backup evidence"
    except OSError:
        return "cannot read verified backup evidence"
    try:
        if len(raw) > 8192:
            raise ValueError
        proof = json.loads(raw)
        if proof["sqlite_integrity"] != "ok" or proof["foreign_key_errors"] != 0:
            raise ValueError
        captured = datetime.fromisoformat(proof["captured_at"])
        verified = datetime.fromisoformat(proof["verified_at"])
        if captured.tzinfo is None or verified.tzinfo is None or not captured <= verified <= now:
            raise ValueError
        if now - captured > MAX_CAPTURE_AGE:
            return "last verified backup captured state over 26 hours ago"
        try:
            failed_at = (control / "last-failure").stat().st_mtime
        except FileNotFoundError:
            pass
        else:
            if captured.timestamp() <= failed_at:
                return "backup failed and no newer capture has passed restore verification"
        if backup_unit_failed():
            return "backup service is unavailable or its last run failed"
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        OverflowError,
        RecursionError,
        subprocess.SubprocessError,
    ):
        return "cannot validate backup health evidence"
    return None


def main(action, control=CONTROL):
    if action == ["record-result"]:
        # The original resume command runs even if recording a failure is impossible.
        if os.environ.get("SERVICE_RESULT") != "success":
            try:
                (control / "last-failure").touch(mode=0o600)
            except OSError:
                print("could not record the backup failure", file=sys.stderr)
                return 1
        return 0
    if action != ["check"]:
        raise SystemExit("usage: backup-health.py {check,record-result}")
    detail = problem(control, datetime.now(UTC))
    if detail:
        print(json.dumps({"healthy": False, "problems": [{"kind": "backup", "detail": detail}]}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
