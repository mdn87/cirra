import hashlib
import hmac
import ipaddress
import json
import os
import plistlib
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cirra import private_transfer as transfer
from cirra.domain import CirraError, canonical, digest
from cirra.locking import change_lock
from cirra.quorum_budget import enrollment_locks
from cirra.store import Store

KEY = b"dedicated synthetic witness secret for transfer tests"
HOST = digest("synthetic enrolled host")


class Owner:
    token = "synthetic-owner"

    def __init__(self, marker):
        self.raw = canonical(marker)
        self.active = True

    def get(self, key):
        assert key == "data/authority"
        return self.raw

    def compare(self, checks, puts):
        assert checks == {"data/authority": self.raw} and puts == {}
        return self.active


def fixture(tmp_path, populated=True):
    state = tmp_path / "state"
    Store(state)
    marker = {
        "host": HOST,
        "epoch": digest("epoch"),
        "tip": digest("tip"),
        "session": digest(["private-session", Owner.token]),
        "floor": int(time.time()),
        "target": None,
    }
    (state / "data-authority.json").write_text(canonical(marker))
    if populated:
        for name in ("witness", "execution-witness"):
            record_name = "f" * 64 + "-" + name
            history = state / "observer" / "history" / record_name
            history.mkdir(parents=True)
            statement = {"example": name}
            body = {
                "statement": statement,
                "hmac_sha256": hmac.digest(KEY, canonical(statement).encode(), "sha256").hex(),
            }
            (history / (digest(body) + ".json")).write_text(canonical(body))
            (state / "observer" / (record_name + ".json")).write_text(canonical(body))
    return state, Owner(marker)


def test_complete_tree_roundtrip_under_the_real_worker_locks(tmp_path):
    state, _ = fixture(tmp_path)
    (state / "private-empty").mkdir()
    private = state / "late-issued-result"
    private.write_bytes(b"\x00 private state")
    private.chmod(0o600)
    archive = tmp_path / "original.zip"
    with enrollment_locks(state):
        receipt = transfer.capture_quiesced(state, archive, KEY.decode())
    target = tmp_path / "new"
    target.mkdir()
    assert transfer.restore_into(archive, target, KEY) == receipt
    assert set(receipt) == {"snapshot_id", "captured_at", "host", "epoch", "tip"}
    assert json.loads((target / "data-restored.json").read_bytes()) == receipt
    with pytest.raises(CirraError, match="cannot refresh"):
        transfer.capture_quiesced(target, tmp_path / "redated.zip", KEY)
    for source in state.rglob("*"):
        copy = target / source.relative_to(state)
        assert (copy.read_bytes() == source.read_bytes()) if source.is_file() else copy.is_dir()
        assert copy.stat().st_mtime_ns == source.stat().st_mtime_ns
    if os.name == "posix":
        assert (target / private.name).stat().st_mode & 0o777 == 0o600
        assert archive.stat().st_mode & 0o777 == 0o600
    with pytest.raises(CirraError, match="empty"):
        transfer.restore_into(archive, state, KEY)
    with pytest.raises(CirraError, match="new destination"):
        transfer.capture_quiesced(state, archive, KEY)
    with pytest.raises(CirraError, match="signature"):
        transfer.restore_into(archive, tmp_path / "invalid", b"x" * 32)
    assert not (tmp_path / "invalid").exists()


@pytest.mark.parametrize(
    "failure", ["sqlite", "foreign-key", "historical", "current", "running", "journal"]
)
def test_capture_rejects_inconsistent_or_unverifiable_state(tmp_path, failure):
    state, _ = fixture(tmp_path)
    if failure == "sqlite":
        (state / "cirra.sqlite3").write_bytes(b"invalid")
    elif failure == "foreign-key":
        with sqlite3.connect(state / "cirra.sqlite3") as db:
            db.execute(
                "INSERT INTO reports(run_id,report_digest,body) VALUES('absent','test','{}')"
            )
    elif failure == "historical":
        path = next((state / "observer" / "history").rglob("*.json"))
        path.rename(path.with_name("0" * 64 + ".json"))
    elif failure == "current":
        next((state / "observer").glob("*-witness.json")).write_text("{}")
    elif failure == "running":
        (state / "audit-queue").mkdir()
        (state / "audit-queue" / "active.json").write_text('{"state":"running"}')
    else:
        (state / "cirra.sqlite3-wal").touch()
    archive = tmp_path / "invalid.zip"
    with pytest.raises(CirraError):
        transfer.capture_quiesced(state, archive, KEY)
    assert not archive.exists()


def test_empty_store_and_signed_manifest_inventory(tmp_path):
    state, _ = fixture(tmp_path, populated=False)
    archive = tmp_path / "initial.zip"
    transfer.capture_quiesced(state, archive, KEY)
    assert transfer.restore_into(archive, tmp_path / "empty-store-copy", KEY)["host"] == HOST
    for failure in ("extra", "replaced", "missing"):
        changed = tmp_path / f"{failure}.zip"
        with zipfile.ZipFile(archive) as original, zipfile.ZipFile(changed, "x") as writer:
            for name in original.namelist():
                if name == "state/cirra.sqlite3" and failure == "missing":
                    continue
                body = original.read(name)
                if name == "state/cirra.sqlite3" and failure == "replaced":
                    body = b"\0" + body[1:]
                writer.writestr(name, body)
            if failure == "extra":
                writer.writestr("state/../../outside", b"unlisted")
        with pytest.raises(CirraError):
            transfer.restore_into(changed, tmp_path / "output", KEY)
        assert not (tmp_path / "outside").exists() and not (tmp_path / "output").exists()


def test_failed_scratch_capacity_preserves_active_state_and_output(tmp_path, monkeypatch):
    state, _ = fixture(tmp_path)
    monkeypatch.setattr(
        transfer.shutil, "disk_usage", lambda _: SimpleNamespace(total=1000, free=5)
    )
    destination = tmp_path / "capture.zip"
    with pytest.raises(CirraError, match="scratch space"):
        transfer.capture_quiesced(state, destination, KEY)
    assert not destination.exists() and (state / "cirra.sqlite3").is_file()


def test_linked_queue_or_new_file_during_capture_is_refused(tmp_path, monkeypatch):
    state, _ = fixture(tmp_path)
    (tmp_path / "other").mkdir()
    try:
        (state / "queue").symlink_to(tmp_path / "other", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation unavailable")
    with pytest.raises(CirraError, match="link"):
        transfer.capture_quiesced(state, tmp_path / "invalid", KEY)
    (state / "queue").unlink()
    copy = transfer._copy

    def changed(*args):
        (state / "new-while-copying").touch()
        return copy(*args)

    monkeypatch.setattr(transfer, "_copy", changed)
    with pytest.raises(CirraError, match="changed"):
        transfer.capture_quiesced(state, tmp_path / "invalid", KEY)
    assert not (tmp_path / "invalid").exists()


class Repository:
    def __init__(self):
        self.records = []
        self.data = {}
        self.interrupt = False
        self.damage = False

    def command(self, args, *, stdin=None, stdout=None):
        if args[0] == "snapshots":
            return json.dumps(self.records).encode()
        if args[0] == "ls":
            return canonical(
                {"type": "file", "path": transfer.RESTIC_PATH, "size": len(self.data[args[1]])}
            ).encode()
        if args[0] == "backup":
            host = args[args.index("--host") + 1]
            tags = [value for idx, value in enumerate(args) if idx and args[idx - 1] == "--tag"]
            assert args[-3:] == ["--stdin", "--stdin-filename", transfer.RESTIC_PATH]
            body = stdin.read()
            identifier = hashlib.sha256(body).hexdigest()
            self.records.append(
                {
                    "time": datetime.fromisoformat(args[args.index("--time") + 1])
                    .replace(tzinfo=UTC)
                    .isoformat(),
                    "hostname": host,
                    "paths": [transfer.RESTIC_PATH],
                    "tags": tags,
                    "id": identifier,
                }
            )
            self.data[identifier] = body
            if self.interrupt:
                self.interrupt = False
                raise CirraError("simulated lost acknowledgement")
            return canonical({"message_type": "summary", "snapshot_id": identifier}).encode()
        assert args[0] == "dump" and args[2] == transfer.RESTIC_PATH
        stdout.write(b"broken" if self.damage else self.data[args[1]])


def approved(monkeypatch):
    value = "sftp:synthetic:/enrolled-cirra"
    monkeypatch.setenv("RESTIC_REPOSITORY", value)
    monkeypatch.setenv("CIRRA_APPROVED_BACKUP_REPOSITORY", value)


def test_retry_after_upload_and_independent_restore_before_elected_receipt(tmp_path, monkeypatch):
    approved(monkeypatch)
    state, owner = fixture(tmp_path)
    repo = Repository()
    monkeypatch.setattr(transfer, "_restic", repo.command)

    def backup():
        return transfer.scheduled_backup(state, HOST, KEY, wait_seconds=0)

    def worker():
        with change_lock(state / "queue" / "locks", "worker", 0):
            return transfer.capture_requested(state, owner, lambda: KEY)

    assert not transfer.capture_pending(state)
    assert (
        transfer.capture_requested(state, owner, lambda: pytest.fail("unexpected lookup")) is None
    )
    assert backup()["status"] == "waiting_for_worker_capture"
    assert transfer.capture_pending(state)
    owner.active = False
    with pytest.raises(CirraError, match="authority"):
        worker()
    owner.active = True
    receipt = worker()
    assert receipt["host"] == HOST
    assert worker() is None
    assert not (state / "data-backup.json").exists()
    repo.interrupt = True
    with pytest.raises(CirraError, match="lost acknowledgement"):
        backup()
    repo.damage = True
    with pytest.raises(CirraError, match="corrupt"):
        backup()
    assert not (state / "data-backup.json").exists() and worker() is None
    repo.damage = False
    assert backup()["status"] == "waiting_for_worker_receipt"
    owner.active = False
    with pytest.raises(CirraError, match="authority"):
        worker()
    assert not (state / "data-backup.json").exists()
    owner.active = True
    assert worker() == receipt
    assert (
        len(repo.records) == 1 and json.loads((state / "data-backup.json").read_bytes()) == receipt
    )
    status = backup()
    assert status["status"] == "current" and not transfer.capture_pending(state)
    assert datetime.fromisoformat(status["next_check"]) - datetime.fromisoformat(
        receipt["captured_at"]
    ) == timedelta(hours=6)
    identifier = repo.records[0]["id"]
    repo.data["0" * 64] = repo.data[identifier]
    for mismatch in ({"hostname": digest("other")}, {"paths": ["/other"]}, {"tags": ["unrelated"]}):
        repo.records.append({**repo.records[0], **mismatch, "id": "0" * 64})
    state.rename(tmp_path / "offline-source")
    archive = tmp_path / "replacement.zip"
    fetched = transfer.fetch_latest(HOST, archive, KEY)
    assert fetched == {"status": "fetched", "restic_snapshot_id": identifier, **receipt}
    assert not (tmp_path / "data-backup.json").exists()
    assert (
        transfer.fetch_latest(
            HOST, tmp_path / "unused", lambda: pytest.fail("unexpected"), known_snapshot=identifier
        )["status"]
        == "unchanged"
    )
    assert not (tmp_path / "unused").exists()
    assert transfer.restore_into(archive, tmp_path / "replacement", KEY) == receipt
    with pytest.raises(CirraError):
        transfer.fetch_latest(digest("other"), tmp_path / "wrong", KEY)
    assert not (tmp_path / "wrong").exists()


def test_repository_binding_and_independent_credential_scope(tmp_path, monkeypatch):
    approved(monkeypatch)
    monkeypatch.setenv("CIRRA_WITNESS_KEY", "must not reach backend")
    monkeypatch.setenv("GITHUB_TOKEN", "must not reach backend")
    assert not {"CIRRA_WITNESS_KEY", "GITHUB_TOKEN"} & transfer._repository_env().keys()
    monkeypatch.setenv("RESTIC_REPOSITORY", "sftp:other:/repo")
    monkeypatch.setattr(transfer, "_restic", lambda *args: pytest.fail("unexpected access"))
    with pytest.raises(CirraError, match="explicitly approved"):
        transfer.fetch_latest(HOST, tmp_path / "no", KEY)
    only = SimpleNamespace(routes={"a": SimpleNamespace(witness_env="WITNESS")})
    assert transfer.configured_witness(only, lambda name: KEY if name == "WITNESS" else None) == KEY
    only.routes["b"] = SimpleNamespace(witness_env="DIFFERENT")
    with pytest.raises(CirraError, match="one configured"):
        transfer.configured_witness(only, lambda name: pytest.fail("unexpected lookup"))
    only.routes = {"review-only": SimpleNamespace(witness_env=None)}
    only.coordination = SimpleNamespace(transfer_witness_env="TRANSFER")
    assert (
        transfer.configured_witness(only, lambda name: KEY if name == "TRANSFER" else None) == KEY
    )
    only.routes["another"] = SimpleNamespace(witness_env="OLD")
    with pytest.raises(CirraError, match="one configured"):
        transfer.configured_witness(only, lambda name: pytest.fail("unexpected lookup"))


def test_worker_waits_for_local_intake_and_refuses_an_expired_cached_receipt(tmp_path, monkeypatch):
    approved(monkeypatch)
    state, owner = fixture(tmp_path)
    assert (
        transfer.scheduled_backup(state, HOST, KEY, wait_seconds=0)["status"]
        == "waiting_for_worker_capture"
    )
    with (
        change_lock(state / "queue" / "locks", "enqueue", 0),
        pytest.raises(CirraError, match="another invocation"),
    ):
        transfer.capture_requested(
            state, owner, lambda: pytest.fail("busy intake resolved a witness")
        )
    assert transfer.capture_requested(state, owner, lambda: KEY)["host"] == HOST
    control = transfer._control(state)
    with (
        change_lock(control / "locks", "restic-backup", 0),
        pytest.raises(CirraError, match="another invocation"),
    ):
        transfer.scheduled_backup(
            state,
            HOST,
            lambda: pytest.fail("competing backup resolved credentials"),
            wait_seconds=0,
        )
    repo = Repository()
    identifier = "c" * 64
    repo.records.append(
        {
            "time": (datetime.now(UTC) - timedelta(hours=25)).isoformat(),
            "id": identifier,
            "hostname": HOST,
            "paths": [transfer.RESTIC_PATH],
            "tags": [transfer.FORMAT],
        }
    )
    monkeypatch.setattr(transfer, "_restic", repo.command)
    with pytest.raises(CirraError, match="current recovery point"):
        transfer.fetch_latest(HOST, tmp_path / "unused", KEY, known_snapshot=identifier)
    assert not (tmp_path / "unused").exists()


def test_mac_schedule_keeps_credentials_and_primary_out_of_program_arguments():
    output = subprocess.check_output(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "macos-private-backup-plist.py"),
            "--python",
            "/app/python",
            "--state-dir",
            "/secure/state",
            "--expected-host",
            HOST,
            "--repository-env",
            "/secure/storage.env",
            "--approval-file",
            "/secure/approved.conf",
            "--witness-file",
            "/secure/witness.env",
            "--logs",
            "/secure/logs",
        ]
    )
    job = plistlib.loads(output)
    assert job["RunAtLoad"] and job["StartInterval"] == 900 and job["Umask"] == 0o077
    assert job["ProgramArguments"][:4] == ["/app/python", "-m", "cirra.private_transfer", "backup"]
    assert set(job["EnvironmentVariables"]) == {"PATH"}


def test_offline_cli_uses_separate_raw_credential_and_requires_frozen_native_donor(tmp_path):
    state, _ = fixture(tmp_path)
    credential = tmp_path / "witness"
    credential.write_bytes(KEY)
    credential.chmod(0o400)
    args = [
        "capture",
        "--state-dir",
        str(state),
        "--archive",
        str(tmp_path / "frozen.zip"),
        "--witness-credential",
        str(credential),
    ]
    with pytest.raises(CirraError, match="freeze"):
        transfer._cli(args)
    marker = json.loads((state / "data-authority.json").read_bytes())
    marker["target"] = digest("other authorized host")
    (state / "data-authority.json").write_text(canonical(marker))
    captured = transfer._cli(args)
    restored = transfer._cli(
        [
            "restore",
            "--archive",
            str(tmp_path / "frozen.zip"),
            "--state-dir",
            str(tmp_path / "scratch"),
            "--witness-credential",
            str(credential),
        ]
    )
    assert restored == captured


def test_real_encrypted_restic_backup_and_source_independent_fetch(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("the enrolled Mac and Linux use POSIX restic source-path metadata")
    restic, server = shutil.which("restic"), shutil.which("rest-server")
    if not (restic and server):
        pytest.skip("optional real restic and rest-server binaries are unavailable")
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    tls_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(tls_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False
        )
        .sign(tls_key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "tls.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        tls_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with socket.socket() as port:
        port.bind(("127.0.0.1", 0))
        endpoint = "127.0.0.1:" + str(port.getsockname()[1])
    for name in list(os.environ):
        if name.startswith("RESTIC_"):
            monkeypatch.delenv(name)
    repo = "rest:https://" + endpoint + "/fixture/"
    monkeypatch.setenv("RESTIC_REPOSITORY", repo)
    monkeypatch.setenv("CIRRA_APPROVED_BACKUP_REPOSITORY", repo)
    recovery_password = uuid.uuid4().hex
    monkeypatch.setenv("RESTIC_PASSWORD", recovery_password)
    monkeypatch.setenv("RESTIC_CACERT", str(cert_file))
    store = tmp_path / "encrypted-offhost-fixture"
    store.mkdir()
    with subprocess.Popen(
        [
            server,
            "--path",
            str(store),
            "--listen",
            endpoint,
            "--no-auth",
            "--tls",
            "--tls-cert",
            str(cert_file),
            "--tls-key",
            str(key_file),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as process:
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", int(endpoint.split(":")[1])), 0.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline or process.poll() is not None:
                        pytest.fail("synthetic TLS storage server failed to start")
                    time.sleep(0.05)
            subprocess.run(
                [restic, "--no-cache", "init"],
                env=transfer._repository_env(),
                check=True,
                capture_output=True,
                timeout=60,
            )
            state, owner = fixture(tmp_path)
            (state / "proof.bin").write_bytes(b"synthetic-private-body-" * 50)
            assert (
                transfer.scheduled_backup(state, HOST, KEY, wait_seconds=0)["status"]
                == "waiting_for_worker_capture"
            )
            with change_lock(state / "queue" / "locks", "worker", 0):
                receipt = transfer.capture_requested(state, owner, lambda: KEY)
            assert (
                transfer.scheduled_backup(state, HOST, KEY, wait_seconds=0)["status"]
                == "waiting_for_worker_receipt"
            )
            assert not (state / "data-backup.json").exists()
            with change_lock(state / "queue" / "locks", "worker", 0):
                assert transfer.capture_requested(state, owner, lambda: KEY) == receipt
            assert (
                transfer.scheduled_backup(state, HOST, KEY, wait_seconds=0)["status"] == "current"
            )
            state.rename(tmp_path / "failed-source")
            monkeypatch.setenv("RESTIC_PASSWORD", "incorrect synthetic password")
            with pytest.raises(CirraError, match="repository operation failed"):
                transfer.fetch_latest(HOST, tmp_path / "failed", KEY)
            assert not (tmp_path / "failed").exists()
            # Reopen the authenticated repository independently, after the source has gone offline.
            monkeypatch.setenv("RESTIC_PASSWORD", recovery_password)
            archive = tmp_path / "independent.zip"
            result = transfer.fetch_latest(HOST, archive, KEY)
            assert result["snapshot_id"] == receipt["snapshot_id"]
            assert result["restic_snapshot_id"] != receipt["snapshot_id"]
            assert transfer.restore_into(archive, tmp_path / "recovered", KEY) == receipt
            assert (
                tmp_path / "recovered" / "proof.bin"
            ).read_bytes() == b"synthetic-private-body-" * 50
            assert not state.exists()
        finally:
            process.terminate()
            process.wait(timeout=10)
