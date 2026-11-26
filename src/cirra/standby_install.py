"""Install a verified private recovery point while the two HTTP origins remain available."""

import hashlib
import json
import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cirra.coordination import connect
from cirra.data_authority import DataAuthority
from cirra.domain import CirraError
from cirra.locking import change_lock
from cirra.publish import sync_directory, write_receipt


def archive_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def refresh(config, directory, credential):
    from cirra.private_transfer import configured_witness, fetch_latest

    inbox = connect(config, directory, credential)
    if inbox is None or config.coordination.recovery_archive is None:
        raise CirraError(
            "standby refresh requires an enrolled service and a private recovery_archive"
        )
    donor = DataAuthority(inbox).expected_donor()
    if donor is None:
        return {"status": "no_transfer_due"}
    cache = config.coordination.recovery_archive
    cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if cache.is_symlink():
        raise CirraError("the private recovery cache must be a regular file")
    metadata = cache.with_name(cache.name + ".receipt.json")
    with change_lock(cache.parent / "locks", cache.name, 0):
        known = None
        try:
            old = json.loads(metadata.read_bytes())
            age = datetime.now(UTC) - datetime.fromisoformat(old["captured_at"])
            if (
                old["host"] == donor
                and timedelta(seconds=-60) <= age < timedelta(days=1)
                and cache.is_file()
                and archive_digest(cache) == old["archive_digest"]
            ):
                known = old["restic_snapshot_id"]
        except (FileNotFoundError, ValueError, TypeError, KeyError):
            pass
        with tempfile.TemporaryDirectory(dir=cache.parent) as staging:
            downloaded = Path(staging) / "verified.zip"
            result = fetch_latest(
                donor, downloaded, configured_witness(config, credential), known_snapshot=known
            )
            if result["status"] == "unchanged":
                return result
            if result["status"] != "fetched" or result["host"] != donor:
                raise CirraError("the recovery archive has an unexpected donor")
            if DataAuthority(inbox).expected_donor() != donor:
                raise CirraError("the data-host authority changed during the transfer")
            metadata_value = {**result, "archive_digest": archive_digest(downloaded)}
            os.replace(downloaded, cache)
            sync_directory(cache.parent)
            write_receipt(metadata, metadata_value, history=False)
            return result


def install(config, directory, credential, archive):
    from cirra.private_transfer import configured_witness, restore_into

    # The operator selects a symlink once. A real directory, including the live
    # primary, is never replaced. All restored roots survive selector changes.
    selector = Path(directory).absolute()
    if not selector.is_symlink():
        raise CirraError("standby installation requires a managed state-directory symlink")
    previous = selector.resolve(strict=True)
    active = connect(config, previous, credential)
    if active is None or DataAuthority(active).expected_donor() is None:
        raise CirraError("the quorum has not selected a donor for this data host")
    archive = Path(archive)
    if not archive.is_file() or archive.is_symlink():
        raise CirraError("the verified recovery archive is unavailable")
    content_id = archive_digest(archive)
    versions = selector.with_name(selector.name + ".restores")
    versions.mkdir(mode=0o700, exist_ok=True)
    if versions.is_symlink():
        raise CirraError("standby versions require a private directory")
    final = versions / content_id
    with change_lock(versions / "locks", "select", 0):
        if not final.exists():
            restored = versions / ("staged-" + uuid.uuid4().hex)
            restore_into(archive, restored, configured_witness(config, credential))
            # Do not install a file that changed during verification.
            if archive_digest(archive) != content_id:
                raise CirraError("the recovery archive changed during installation")
            restored.rename(final)
            sync_directory(versions)
        if final.is_symlink() or not final.is_dir():
            raise CirraError("invalid private restore destination")
        incoming = connect(config, final, credential)
        if incoming is None:
            raise CirraError("the recovery archive predates coordination enrollment")
        incoming.check()
        authority = DataAuthority(incoming)
        with change_lock(active.queue.directory / "locks", "worker", 0):
            if selector.resolve(strict=True) != previous or not authority.accepts_restore():
                raise CirraError("the restored private state is ineligible for this data host")
            receipt = incoming.queue.read(final / "data-restored.json")
            old_receipt_path = previous / "data-restored.json"
            if previous == final:
                return {"installed": False, "snapshot_id": receipt["snapshot_id"]}
            if old_receipt_path.is_file():
                old = active.queue.read(old_receipt_path)
                if (
                    old["epoch"] == receipt["epoch"]
                    and old["host"] == receipt["host"]
                    and datetime.fromisoformat(old["captured_at"])
                    >= datetime.fromisoformat(receipt["captured_at"])
                    and DataAuthority(active).accepts_restore()
                ):
                    return {"installed": False, "snapshot_id": old["snapshot_id"]}
            temporary = selector.with_name("." + selector.name + "-" + uuid.uuid4().hex)
            temporary.symlink_to(final, target_is_directory=True)
            os.replace(temporary, selector)
            sync_directory(selector.parent)
    return {"installed": True, "snapshot_id": receipt["snapshot_id"]}
