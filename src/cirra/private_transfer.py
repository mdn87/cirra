"""Authenticated, complete private-state transfers between the two authorized data hosts."""

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
import zipfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cirra.data_authority import AUTHORITY, HEX, valid_authority
from cirra.domain import CirraError, canonical, digest
from cirra.locking import change_lock, read_locked_file
from cirra.publish import sync_directory, write_receipt
from cirra.store import SCHEMA_VERSION

FORMAT = "cirra-private-authority-v1"
WITNESS = re.compile(r"[0-9a-f]{64}-(?:execution-)?witness")
MAX_MANIFEST = 64 * 1024 * 1024
CHUNK = 1024 * 1024
RESTIC_PATH = "/cirra-private-transfer.zip"
BACKUP_PERIOD = timedelta(hours=6)
CHECK_PERIOD = timedelta(minutes=15)


def _key(value):
    if isinstance(value, str):
        value = value.encode()
    if not isinstance(value, bytes) or len(value) < 32:
        raise CirraError("a separate witness credential of at least 32 bytes is required")
    return value


def configured_witness(config, credential):
    names = {route.witness_env for route in config.routes.values() if route.witness_env}
    explicit = getattr(getattr(config, "coordination", None), "transfer_witness_env", None)
    if explicit:
        names.add(explicit)
    if len(names) != 1:
        raise CirraError("private transfer requires one configured witness credential reference")
    return _key(credential(names.pop()))


def _utc():
    return datetime.now(UTC)


def _timestamp(value):
    if not isinstance(value, str):
        raise CirraError("the private transfer timestamp is invalid")
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.utcoffset() != UTC.utcoffset(None):
            raise ValueError
        return timestamp
    except (ValueError, OverflowError):
        raise CirraError("the private transfer timestamp must be UTC") from None


def _authority(state):
    path = state / "data-authority.json"
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError
        record = json.loads(path.read_bytes())
    except (OSError, ValueError):
        raise CirraError("the local data authority marker is unavailable or invalid") from None
    if not valid_authority(record):
        raise CirraError("the local data authority marker is invalid")
    return record


def _projection(record):
    return {name: record[name] for name in ("host", "epoch", "tip")}


def _component(value):
    # A signed name must identify the same single component on both data hosts.
    if (
        not value
        or value in {".", ".."}
        or value.endswith((" ", "."))
        or any(ord(c) < 32 or c in '/\\:<>"|?*' for c in value)
        or re.fullmatch(
            r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[0-9¹²³]",
            value.partition(".")[0].rstrip().upper(),
        )
    ):
        raise CirraError("private transfer contains a nonportable path")


def _fingerprint(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _inventory(state):
    root = state.lstat()
    if not stat.S_ISDIR(root.st_mode):
        raise CirraError("private state requires a real directory")
    entries = {}
    pending = [state]
    while pending:
        parent = pending.pop()
        names = set()
        for child in parent.iterdir():
            _component(child.name)
            name = unicodedata.normalize("NFC", child.name).casefold()
            if name in names:
                raise CirraError("private transfer contains colliding path names")
            names.add(name)
            info = child.lstat()
            if stat.S_IMODE(info.st_mode) & ~(0o3777 if stat.S_ISDIR(info.st_mode) else 0o777):
                raise CirraError("private state contains unsupported special permission bits")
            if (
                info.st_dev != root.st_dev
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise CirraError("private state contains a mount or filesystem link")
            if stat.S_ISDIR(info.st_mode):
                pending.append(child)
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise CirraError("private state contains a link or a special file")
            entries[child.relative_to(state).as_posix()] = _fingerprint(info)
    return entries


def _verify_state(state, key):
    db = state / "cirra.sqlite3"
    if not db.is_file() or any(
        Path(str(db) + suffix).exists() for suffix in ("-journal", "-shm", "-wal")
    ):
        raise CirraError("the private database must be a checkpointed regular SQLite file")
    try:
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
            if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise CirraError("private SQLite integrity check failed")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise CirraError("private SQLite foreign-key check failed")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if (
                version != SCHEMA_VERSION
                or not {"claims", "attempts", "reports", "audits"} <= tables
            ):
                raise CirraError("the private SQLite schema is incompatible")
    except sqlite3.Error:
        raise CirraError("the private SQLite database could not be verified") from None
    for name in ("queue", "audit-queue"):
        queue = state / name
        if queue.exists() and not queue.is_dir():
            raise CirraError("a private queue root is not a directory")
        for job in queue.glob("*.json"):
            if not job.is_file():
                raise CirraError("a private queue record is not a file")
            try:
                data = json.loads(job.read_bytes())
            except (ValueError, RecursionError):
                continue  # The scheduler skips malformed work. Preserve its exact bytes.
            if isinstance(data, dict) and data.get("state") == "running":
                raise CirraError("private capture requires a completed or idle worker pass")
    observer = state / "observer"
    history = observer / "history"
    current = list(observer.glob("*-witness.json"))
    old = []
    for folder in history.glob("*-witness"):
        if not folder.is_dir() or not WITNESS.fullmatch(folder.name):
            raise CirraError("the private historical witness directory is invalid")
        old.extend(folder.glob("*.json"))
    for historical, paths in ((False, current), (True, old)):
        for path in paths:
            if (
                not path.is_file()
                or path.stat().st_size > CHUNK
                or (not historical and not WITNESS.fullmatch(path.stem))
            ):
                raise CirraError("the private witness filename or size is invalid")
            try:
                body = json.loads(path.read_bytes())
                if set(body) != {"statement", "hmac_sha256"}:
                    raise ValueError
                expected = hmac.digest(key, canonical(body["statement"]).encode(), "sha256").hex()
                if not hmac.compare_digest(expected, body["hmac_sha256"]):
                    raise ValueError
                if historical and path.stem != digest(body):
                    raise ValueError
            except (TypeError, ValueError, RecursionError):
                raise CirraError(
                    "a private witness signature or historical digest is invalid"
                ) from None


def _signature(key, manifest):
    return hmac.digest(key, FORMAT.encode() + b"\0" + canonical(manifest).encode(), "sha256").hex()


def _receipt(manifest):
    return {
        "snapshot_id": digest(manifest),
        "captured_at": manifest["captured_at"],
        **manifest["authority"],
    }


def _copy(source, destination, expected_size):
    sha = hashlib.sha256()
    count = 0
    while chunk := source.read(CHUNK):
        if count + len(chunk) > expected_size:
            raise CirraError("a private file grew beyond its inventoried size")
        destination.write(chunk)
        sha.update(chunk)
        count += len(chunk)
    return count, sha.hexdigest()


def _require_space(directory, required):
    volume = shutil.disk_usage(directory)
    # Keep the existing standby's 2 GiB margin, scaled down for a dedicated small tmpfs.
    reserve = min(2 * 1024**3, volume.total // 16)
    if volume.free < required + reserve:
        raise CirraError("insufficient private scratch space to safely complete this transfer")


def _new_file(path):
    if os.path.lexists(path):
        raise CirraError("private transfer requires a new destination file")


def _publish_file(temporary, destination):
    # Linking a completely written file is atomic and cannot replace a concurrent destination.
    with temporary.open("r+b") as stream:
        os.fsync(stream.fileno())
    try:
        os.link(temporary, destination)
    except FileExistsError:
        raise CirraError("a private transfer destination already exists") from None
    sync_directory(destination.parent)


def capture_quiesced(state, archive, witness_key):
    """Capture the complete tree while the caller owns the worker and both enqueue locks."""
    state = Path(state).resolve(strict=True)
    if os.name == "posix" and os.geteuid() == 0 and state.stat().st_uid != 0:
        raise CirraError("run private capture as the service account that owns this state")
    archive = Path(archive).absolute()
    if archive.resolve().is_relative_to(state):
        raise CirraError("the private transfer archive must be outside the source state")
    _new_file(archive)
    key = _key(witness_key)
    captured = _utc().isoformat()
    before = _inventory(state)
    estimated = sum(
        (details[3] if stat.S_ISREG(details[2]) else 0) + 1024 + 8 * len(name.encode())
        for name, details in before.items()
    )
    _require_space(archive.parent, estimated)
    _verify_state(state, key)
    authority = _authority(state)
    restored = _record(state / "data-restored.json")
    if restored and restored.get("epoch") == authority["epoch"]:
        raise CirraError("a restored copy cannot refresh its original donor's capture time")
    entries = {}
    with tempfile.TemporaryDirectory(dir=archive.parent) as scratch:
        temporary = Path(scratch) / "capture.zip"
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_STORED) as bundle:
            for name, details in sorted(before.items()):
                mode = details[2]
                entry = {
                    "mode": stat.S_IMODE(mode),
                    "mtime_ns": details[4],
                    "type": "dir" if stat.S_ISDIR(mode) else "file",
                }
                if stat.S_ISREG(mode):
                    with read_locked_file(state / name) as source:
                        if _fingerprint(os.fstat(source.fileno())) != details:
                            raise CirraError("private state changed while starting capture")
                        with bundle.open("state/" + name, "w", force_zip64=True) as target:
                            size, sha = _copy(source, target, details[3])
                        if size != details[3] or _fingerprint(os.fstat(source.fileno())) != details:
                            raise CirraError("private state changed during capture")
                    entry.update(size=size, sha256=sha)
                entries[name] = entry
            manifest = {
                "format": FORMAT,
                "captured_at": captured,
                "authority": _projection(authority),
                "entries": entries,
            }
            envelope = canonical(
                {"manifest": manifest, "signature": _signature(key, manifest)}
            ).encode()
            if len(envelope) > MAX_MANIFEST:
                raise CirraError("private transfer manifest exceeds its supported limit")
            bundle.writestr("manifest.json", envelope)
        if before != _inventory(state):
            raise CirraError("private state changed during capture; retry at a completed pass")
        temporary.chmod(0o600)
        _publish_file(temporary, archive)
    return _receipt(manifest)


def _read_manifest(bundle, key):
    infos = bundle.infolist()
    by_name = {info.filename: info for info in infos}
    header = by_name.get("manifest.json")
    if (
        header is None
        or len(by_name) != len(infos)
        or header.file_size > MAX_MANIFEST
        or any(
            info.orig_filename != info.filename
            or info.compress_type != zipfile.ZIP_STORED
            or info.flag_bits & 1
            for info in infos
        )
    ):
        raise CirraError("the private transfer archive layout is invalid")
    try:
        envelope = json.loads(bundle.read(header))
        manifest = envelope["manifest"]
        if set(envelope) != {"manifest", "signature"} or not hmac.compare_digest(
            _signature(key, manifest), envelope["signature"]
        ):
            raise CirraError("the private transfer manifest signature is invalid")
        if (
            set(manifest) != {"format", "captured_at", "authority", "entries"}
            or manifest["format"] != FORMAT
            or not isinstance(manifest["entries"], dict)
            or set(manifest["authority"]) != {"host", "epoch", "tip"}
            or not all(HEX.fullmatch(v) for v in manifest["authority"].values())
        ):
            raise ValueError
        _timestamp(manifest["captured_at"])
        names = set()
        files = {"manifest.json"}
        entries = manifest["entries"]
        for name, entry in entries.items():
            parts = name.split("/")
            for part in parts:
                _component(part)
            portable = unicodedata.normalize("NFC", name).casefold()
            if portable in names or (
                len(parts) > 1 and entries.get("/".join(parts[:-1]), {}).get("type") != "dir"
            ):
                raise ValueError
            names.add(portable)
            allowed = 0o3777 if entry["type"] == "dir" else 0o777
            if type(entry["mode"]) is not int or not 0 <= entry["mode"] <= allowed:
                raise ValueError
            if type(entry["mtime_ns"]) is not int or not -(2**63) <= entry["mtime_ns"] < 2**63:
                raise ValueError
            if entry["type"] == "dir" and set(entry) == {"mode", "mtime_ns", "type"}:
                continue
            member = "state/" + name
            if (
                entry["type"] != "file"
                or set(entry) != {"mode", "mtime_ns", "type", "sha256", "size"}
                or type(entry["size"]) is not int
                or entry["size"] != by_name[member].file_size
                or not HEX.fullmatch(entry["sha256"])
            ):
                raise ValueError
            files.add(member)
        if files != set(by_name):
            raise ValueError
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
        raise CirraError("the private transfer manifest is invalid") from None
    return manifest


def _restore(archive, staged, key):
    try:
        with zipfile.ZipFile(archive) as bundle:
            manifest = _read_manifest(bundle, key)
            entries = manifest["entries"]
            _require_space(staged, sum(entry.get("size", 0) + 4096 for entry in entries.values()))
            for name, entry in sorted(entries.items()):
                path = staged / name
                if entry["type"] == "dir":
                    path.mkdir(mode=0o700)
                    continue
                with bundle.open("state/" + name) as source, path.open("xb") as target:
                    size, sha = _copy(source, target, entry["size"])
                    target.flush()
                    os.fsync(target.fileno())
                if size != entry["size"] or sha != entry["sha256"]:
                    raise CirraError("a restored private file differs from its signed digest")
            _verify_state(staged, key)
            if _projection(_authority(staged)) != manifest["authority"]:
                raise CirraError("the restored authority differs from the signed manifest")
            receipt = _receipt(manifest)
            write_receipt(staged / "data-restored.json", receipt, history=False)
            for name, entry in sorted(entries.items(), reverse=True):
                path = staged / name
                if name != "data-restored.json":
                    os.utime(path, ns=(entry["mtime_ns"], entry["mtime_ns"]))
                path.chmod(entry["mode"])
                if entry["type"] == "dir":
                    sync_directory(path)
            sync_directory(staged)
            return receipt
    except (zipfile.BadZipFile, EOFError):
        raise CirraError("the private transfer archive is incomplete or corrupt") from None


def _empty_target(path):
    if not os.path.lexists(path):
        return None
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink() or any(path.iterdir()):
        raise CirraError("private restore requires an absent or empty real destination directory")
    return info.st_dev, info.st_ino


def restore_into(archive, new_empty_state, witness_key):
    """Validate in new private scratch before exposing a restored tree to its caller."""
    archive = Path(archive).absolute()
    if not stat.S_ISREG(archive.lstat().st_mode):
        raise CirraError("the private archive must be a regular file")
    target = Path(new_empty_state).absolute()
    identity = _empty_target(target)
    with tempfile.TemporaryDirectory(dir=target.parent) as scratch:
        staged = Path(scratch) / "state"
        staged.mkdir(mode=0o700)
        receipt = _restore(archive, staged, _key(witness_key))
        if _empty_target(target) != identity:
            raise CirraError("the private restore destination changed during verification")
        if os.name == "nt" and identity is not None:
            target.rmdir()  # Windows rename requires an absent target. rmdir rejects any contents.
        staged.rename(target)
        sync_directory(target.parent)
    return receipt


def _control(root):
    return root.with_name(root.name + ".private-transfer")


def _private_directory(path):
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or (os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077))
    ):
        raise CirraError("private transfer control requires its owner's private directory")


def _record(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > CHUNK:
        raise CirraError("a private transfer control record is invalid")
    try:
        data = json.loads(path.read_bytes())
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, RecursionError):
        raise CirraError("a private transfer control record is invalid") from None


def _request(control):
    record = _record(control / "request.json")
    if record is None:
        return None
    if (
        set(record) != {"id", "created_at", "host", "epoch"}
        or not isinstance(record["id"], str)
        or not re.fullmatch(r"[0-9a-f]{32}", record["id"])
        or not all(
            isinstance(record[n], str) and HEX.fullmatch(record[n]) for n in ("host", "epoch")
        )
    ):
        raise CirraError("the private capture request is invalid")
    _timestamp(record["created_at"])
    return record


def _check_live(owner, authority):
    raw = canonical(authority)
    if (
        authority["target"] is not None
        or authority["session"] != digest(["private-session", owner.token])
        or owner.get(AUTHORITY) != raw
        or not owner.compare({AUTHORITY: raw}, {})
    ):
        raise CirraError("the active data authority changed during private transfer")


def _matches_tenure(receipt, authority):
    return (
        isinstance(receipt, dict)
        and set(receipt) == {"snapshot_id", "host", "epoch", "tip", "captured_at"}
        and all(
            isinstance(receipt[k], str) and HEX.fullmatch(receipt[k])
            for k in ("snapshot_id", "host", "epoch", "tip")
        )
        and all(receipt[k] == authority[k] for k in ("host", "epoch"))
    )


def _check_ack(verified, ready, authority):
    if (
        not _matches_tenure(ready, authority)
        or set(verified) != {"restic_snapshot_id", "receipt"}
        or verified["receipt"] != ready
        or not isinstance(verified["restic_snapshot_id"], str)
        or not HEX.fullmatch(verified["restic_snapshot_id"])
    ):
        raise CirraError("the independent restore differs from the owner's capture")


def capture_pending(root):
    return os.path.lexists(_control(Path(root).resolve(strict=True)) / "request.json")


def capture_requested(root, owner, witness_key=None):
    """Capture under existing worker ownership after a completed or idle pass."""
    root = Path(root).resolve(strict=True)
    control = _control(root)
    if not os.path.lexists(control / "request.json"):
        return None
    _private_directory(control)
    request = _request(control)
    if request is None:
        return None
    authority = _authority(root)
    if (
        any(request[k] != authority[k] for k in ("host", "epoch"))
        or authority["target"] is not None
    ):
        return None
    request_id = request["id"]
    ready_path = control / (request_id + ".ready.json")
    ready = _record(ready_path)
    verified = _record(control / (request_id + ".verified.json"))
    if ready is not None and verified is None:
        return None
    if verified is not None:
        _check_ack(verified, ready, authority)
        if _record(root / "data-backup.json") == ready:
            return ready
    with (
        change_lock(root / "queue" / "locks", "enqueue", 0),
        change_lock(root / "audit-queue" / "locks", "audit-queue", 0),
    ):
        if _request(control) != request:
            return None
        _check_live(owner, authority)
        if verified is not None:
            if not _fresh(ready):
                raise CirraError("the independently restored capture has expired")
            _check_live(owner, authority)
            if _record(root / "data-backup.json") != ready:
                write_receipt(root / "data-backup.json", ready, history=False)
            return ready
        if not callable(witness_key):
            raise CirraError("the active worker requires its configured witness for capture")
        archive = control / (request_id + ".zip")
        # A crash after publishing the ZIP can only leave an unacknowledged artifact;
        # authenticate it against this request's tenure before its ready record is exposed.
        if archive.exists():
            with tempfile.TemporaryDirectory(dir=control) as scratch:
                ready = restore_into(archive, Path(scratch) / "verify", witness_key())
            if any(ready[k] != authority[k] for k in ("epoch", "host")) or _timestamp(
                ready["captured_at"]
            ) < _timestamp(request["created_at"]):
                raise CirraError("the interrupted private capture belongs to another request")
        else:
            ready = capture_quiesced(root, archive, witness_key())
        _check_live(owner, authority)
        write_receipt(ready_path, ready, history=False)
        return ready


def _repository_env():
    approved = os.environ.get("CIRRA_APPROVED_BACKUP_REPOSITORY", "")
    if (
        not approved
        or approved != os.environ.get("RESTIC_REPOSITORY")
        or not approved.startswith(("sftp:", "rest:https://"))
    ):
        raise CirraError("restic must use the explicitly approved off-host repository")
    allowed = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "USERPROFILE",
        "SystemRoot",
        "SYSTEMROOT",
        "TMP",
        "TEMP",
        "TMPDIR",
        "SSH_AUTH_SOCK",
        "RESTIC_REPOSITORY",
        "RESTIC_PASSWORD",
        "RESTIC_PASSWORD_FILE",
        "RESTIC_CACERT",
        "RESTIC_TLS_CLIENT_CERT",
        "RESTIC_REST_USERNAME",
        "RESTIC_REST_PASSWORD",
    }
    return {"TZ": "UTC", **{name: os.environ[name] for name in allowed if name in os.environ}}


def _restic(args, *, stdin=None, stdout=None):
    try:
        return subprocess.run(
            ["restic", "--no-cache", "--json", "--retry-lock", "5s", *args],
            stdin=stdin if stdin is not None else subprocess.DEVNULL,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_repository_env(),
            timeout=600,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        # Backend diagnostics may contain network locations, paths, or credentials.
        raise CirraError("the approved encrypted repository operation failed") from None


def _host(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise CirraError("private transfer requires the exact donor host digest")
    return value


def _snapshots(host, *options):
    try:
        records = json.loads(
            _restic(["snapshots", "--host", _host(host), "--tag", FORMAT, *options])
        )
        if not isinstance(records, list):
            raise ValueError
        matches = []
        for record in records:
            if (
                record.get("hostname") == host
                and FORMAT in record.get("tags", [])
                and record.get("paths") == [RESTIC_PATH]
            ):
                if not HEX.fullmatch(record["id"]):
                    raise ValueError
                stamp = datetime.fromisoformat(record["time"])
                if stamp.utcoffset() is None:
                    raise ValueError
                matches.append((stamp, record["id"], record["tags"]))
        return sorted(matches, reverse=True)
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
        raise CirraError("restic returned invalid private snapshot metadata") from None


def _fresh(receipt, max_age=timedelta(hours=24)):
    age = _utc() - _timestamp(receipt["captured_at"])
    return -timedelta(minutes=1) <= age < max_age


def _download(snapshot_id, host, destination, key):
    selected = [
        stamp for stamp, identifier, _ in _snapshots(host, snapshot_id) if identifier == snapshot_id
    ]
    if len(selected) != 1:
        raise CirraError("the exact restic snapshot does not match its donor, tag and source path")
    try:
        listing = _restic(["ls", snapshot_id, RESTIC_PATH]).splitlines()
        sizes = [
            node["size"]
            for line in listing
            if (node := json.loads(line)).get("type") == "file" and node.get("path") == RESTIC_PATH
        ]
        if len(sizes) != 1 or type(sizes[0]) is not int or sizes[0] < 1:
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise CirraError("the encrypted snapshot has no single portable archive size") from None
    _require_space(destination.parent, sizes[0])
    with destination.open("xb") as stream:
        _restic(["dump", snapshot_id, RESTIC_PATH], stdout=stream)
    if destination.stat().st_size != sizes[0]:
        raise CirraError("the downloaded private archive is incomplete or corrupt")
    destination.chmod(0o600)
    with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
        receipt = restore_into(destination, Path(scratch) / "verified", key)
    if receipt["host"] != host or not _fresh(receipt):
        raise CirraError("the signed private recovery point has expired or belongs to another host")
    if _timestamp(receipt["captured_at"]).replace(microsecond=0) != selected[0]:
        raise CirraError("the encrypted snapshot time differs from its signed capture time")
    return receipt


def fetch_latest(expected_host, archive, witness_key, *, known_snapshot=None):
    """Fetch a verified donor archive. A previously authenticated identical cache ID skips I/O."""
    _repository_env()
    host = _host(expected_host)
    matches = _snapshots(host)
    if not matches:
        raise CirraError(
            "the approved repository contains no private snapshot for the selected donor"
        )
    stamp, snapshot_id, _ = matches[0]
    if not -timedelta(minutes=1) <= _utc() - stamp < timedelta(hours=24):
        raise CirraError("the approved donor has no current recovery point")
    if known_snapshot == snapshot_id:
        return {"status": "unchanged", "restic_snapshot_id": snapshot_id}
    archive = Path(archive).absolute()
    _new_file(archive)
    with tempfile.TemporaryDirectory(dir=archive.parent) as scratch:
        payload = Path(scratch) / "download.zip"
        key = _key(witness_key() if callable(witness_key) else witness_key)
        receipt = _download(snapshot_id, host, payload, key)
        _publish_file(payload, archive)
    return {"status": "fetched", "restic_snapshot_id": snapshot_id, **receipt}


def _offhost_verify(control, request, ready, witness_key):
    request_id = request["id"]
    host = request["host"]
    tag = "cirra-capture-" + ready["snapshot_id"]
    matches = [identifier for _, identifier, tags in _snapshots(host) if tag in tags]
    if matches:
        snapshot_id = matches[0]  # A previous upload can finish before the process sees its reply.
    else:
        archive = control / (request_id + ".zip")
        if not stat.S_ISREG(archive.lstat().st_mode):
            raise CirraError("the source capture is not a regular private archive")
        with archive.open("rb") as stream:
            output = _restic(
                [
                    "backup",
                    "--host",
                    host,
                    "--tag",
                    FORMAT,
                    "--tag",
                    tag,
                    "--time",
                    _timestamp(ready["captured_at"]).strftime("%Y-%m-%d %H:%M:%S"),
                    "--stdin",
                    "--stdin-filename",
                    RESTIC_PATH,
                ],
                stdin=stream,
            )
        try:
            summaries = [
                event["snapshot_id"]
                for line in output.splitlines()
                if (event := json.loads(line)).get("message_type") == "summary"
            ]
            if len(summaries) != 1 or not HEX.fullmatch(summaries[0]):
                raise ValueError
            snapshot_id = summaries[0]
        except (ValueError, TypeError, KeyError, AttributeError):
            raise CirraError("restic did not acknowledge one complete private snapshot") from None
    with tempfile.TemporaryDirectory(dir=control) as scratch:
        recovered = _download(snapshot_id, host, Path(scratch) / "independent.zip", witness_key)
    if recovered != ready:
        raise CirraError("the independent off-host restore differs from the original capture")
    return {"restic_snapshot_id": snapshot_id, "receipt": recovered}


def _response(status, next_check, **fields):
    return {"status": status, "next_check": next_check.isoformat(), **fields}


def _finished(control, request):
    # Remove only these operation-owned transient names after the worker durably acknowledged them.
    (control / "request.json").unlink()
    sync_directory(control)
    for suffix in (".zip", ".ready.json", ".verified.json"):
        (control / (request["id"] + suffix)).unlink(missing_ok=True)


def _new_request(control, authority):
    request = {
        "id": uuid.uuid4().hex,
        "created_at": _utc().isoformat(),
        "host": authority["host"],
        "epoch": authority["epoch"],
    }
    write_receipt(control / "request.json", request, history=False)
    return request


def scheduled_backup(state, expected_host, witness_key, *, wait_seconds=120):
    """Local backup agent for either data host; the worker owns capture and receipt installation."""
    _repository_env()
    host = _host(expected_host)
    if not isinstance(wait_seconds, (int, float)) or not 0 <= wait_seconds <= 600:
        raise CirraError("private capture wait must be between zero and 600 seconds")
    state = Path(state).resolve(strict=True)
    if os.name == "posix" and os.geteuid() == 0 and state.stat().st_uid != 0:
        raise CirraError("run the private backup agent as the service account that owns this state")
    authority = _authority(state)
    if authority["host"] != host or authority["target"] is not None:
        return _response("standby", _utc() + CHECK_PERIOD)
    control = _control(state)
    control.mkdir(mode=0o700, exist_ok=True)
    _private_directory(control)
    with change_lock(control / "locks", "restic-backup", 0):
        request = _request(control)
        if request and any(request[k] != authority[k] for k in ("host", "epoch")):
            # Preserve the old tenure's work for inspection; a new owner must use a new request.
            old = control / ("superseded-" + request["id"] + ".json")
            if old.exists():
                raise CirraError("a previous tenure has conflicting private capture records")
            (control / "request.json").rename(old)
            sync_directory(control)
            request = None
        last = _record(state / "data-backup.json")
        if request is not None:
            verified = _record(control / (request["id"] + ".verified.json"))
            if verified is not None:
                completed = _record(control / (request["id"] + ".ready.json"))
                _check_ack(verified, completed, authority)
                if completed == last:
                    _finished(control, request)
                    request = None
        if request is None and _matches_tenure(last, authority) and _fresh(last, BACKUP_PERIOD):
            return _response(
                "current",
                _timestamp(last["captured_at"]) + BACKUP_PERIOD,
                snapshot_id=last["snapshot_id"],
            )
        if request is None:
            request = _new_request(control, authority)
        identifier = request["id"]
        deadline = time.monotonic() + wait_seconds
        ready = _record(control / (identifier + ".ready.json"))
        while ready is None and time.monotonic() < deadline:
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
            ready = _record(control / (identifier + ".ready.json"))
        if ready is None:
            return _response("waiting_for_worker_capture", _utc() + CHECK_PERIOD)
        if not _matches_tenure(ready, authority):
            raise CirraError("the worker capture does not match the requested authority")
        if not _fresh(ready):
            # An outage may span the RPO. Request a new point; retain the old record and payload.
            (control / "request.json").rename(control / ("expired-" + identifier + ".json"))
            sync_directory(control)
            _new_request(control, authority)
            return _response("waiting_for_worker_capture", _utc() + CHECK_PERIOD)
        verified_path = control / (identifier + ".verified.json")
        verified = _record(verified_path)
        if verified is None:
            key = _key(witness_key() if callable(witness_key) else witness_key)
            verified = _offhost_verify(control, request, ready, key)
            write_receipt(verified_path, verified, history=False)
        _check_ack(verified, ready, authority)
        while _record(state / "data-backup.json") != ready and time.monotonic() < deadline:
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        if _record(state / "data-backup.json") != ready:
            return _response("waiting_for_worker_receipt", _utc() + CHECK_PERIOD)
        _finished(control, request)
        return _response(
            "verified",
            _timestamp(ready["captured_at"]) + BACKUP_PERIOD,
            restic_snapshot_id=verified["restic_snapshot_id"],
            **ready,
        )


def _credential_bytes(path):
    path = Path(path)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > 65536
        or (os.name == "posix" and (info.st_mode & 0o077 or info.st_uid not in (0, os.geteuid())))
    ):
        raise CirraError("transfer credentials require a private regular configuration file")
    with read_locked_file(path) as source:
        if _fingerprint(os.fstat(source.fileno())) != _fingerprint(info):
            raise CirraError("the transfer credential file changed while opening it")
        value = source.read(65537)
        if len(value) > 65536:
            raise CirraError("the transfer credential exceeds its supported size")
    return value


def _private_env(path, names):
    values = {}
    for line in _credential_bytes(path).decode("utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        if not sep or name not in names:
            continue
        if name in values:
            raise CirraError("a private transfer configuration setting is ambiguous")
        value = value.strip()
        if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
            value = value[1:-1]
        values[name] = value
    return values


def _socket_key(path):
    info = path.lstat()
    if (
        os.name != "posix"
        or not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise CirraError("the witness socket must be private and owned by the recovery operator")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(30)
        channel.connect(str(path))
        with channel.makefile("rb") as stream:
            key = stream.read(65537)
    if len(key) > 65536:
        raise CirraError("the witness credential returned by its socket is invalid")
    return _key(key)


def _cli(argv=None):
    from cirra.quorum_budget import enrollment_locks

    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="action", required=True)
    for action in ("backup", "fetch-latest", "capture", "restore"):
        mode = modes.add_parser(action)
        witness = mode.add_mutually_exclusive_group(required=True)
        witness.add_argument("--witness-file", type=Path)
        witness.add_argument("--witness-socket", type=Path)
        witness.add_argument("--witness-credential", type=Path, help="raw systemd credential")
        mode.add_argument("--witness-name", default="CIRRA_WITNESS_KEY")
        if action != "fetch-latest":
            mode.add_argument("--state-dir", required=True, type=Path)
        if action != "backup":
            mode.add_argument("--archive", required=True, type=Path)
        if action in ("backup", "fetch-latest"):
            mode.add_argument("--expected-host", required=True)
            mode.add_argument("--repository-env", type=Path)
            mode.add_argument("--approval-file", type=Path)
        if action == "backup":
            mode.add_argument("--wait-seconds", type=int, default=120)
    args = parser.parse_args(argv)
    if hasattr(args, "state_dir"):
        state = args.state_dir.resolve()
        if any(
            path and path.resolve().is_relative_to(state)
            for path in (
                args.witness_socket or args.witness_credential or args.witness_file,
                getattr(args, "repository_env", None),
                getattr(args, "approval_file", None),
            )
        ):
            raise CirraError("private transfer credentials must remain outside the state tree")

    def witness():
        if args.witness_socket:
            return _socket_key(args.witness_socket)
        if args.witness_credential:
            return _key(_credential_bytes(args.witness_credential))
        return _key(_private_env(args.witness_file, {args.witness_name}).get(args.witness_name))

    if args.action in ("backup", "fetch-latest"):
        if args.repository_env:
            values = _private_env(
                args.repository_env,
                {
                    "RESTIC_REPOSITORY",
                    "RESTIC_PASSWORD",
                    "RESTIC_PASSWORD_FILE",
                    "RESTIC_CACERT",
                    "RESTIC_TLS_CLIENT_CERT",
                    "RESTIC_REST_USERNAME",
                    "RESTIC_REST_PASSWORD",
                },
            )
            if not values.get("RESTIC_REPOSITORY"):
                raise CirraError("the repository environment must name its storage target")
            os.environ.update(values)
        if args.approval_file:
            approval = _private_env(args.approval_file, {"CIRRA_APPROVED_BACKUP_REPOSITORY"})
            if not approval.get("CIRRA_APPROVED_BACKUP_REPOSITORY"):
                raise CirraError("the repository approval file must name its approved target")
            os.environ.update(approval)
    if args.action == "backup":
        return scheduled_backup(
            args.state_dir, args.expected_host, witness, wait_seconds=args.wait_seconds
        )
    if args.action == "fetch-latest":
        return fetch_latest(args.expected_host, args.archive, witness)
    if args.action == "restore":
        return restore_into(args.archive, args.state_dir, witness())
    with enrollment_locks(args.state_dir):
        if _authority(args.state_dir.resolve())["target"] is None:
            raise CirraError(
                "offline capture requires the current donor to freeze its transfer first"
            )
        return capture_quiesced(args.state_dir, args.archive, witness())


def main(argv=None):
    os.umask(0o077)
    try:
        print(json.dumps(_cli(argv), sort_keys=True))
        return 0
    except CirraError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, UnicodeError, zipfile.BadZipFile):
        print("private transfer could not read or commit its protected files", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
