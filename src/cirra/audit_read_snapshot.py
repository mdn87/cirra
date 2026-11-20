"""Cache the v1 content digest with a private, disposable index of receipt metadata.

Only Cirra's atomic receipt replacements change the two published directories. Check
their revisions and SQLite's data_version on both sides of each read. No SQLite
transaction on the service database spans a directory walk or receipt read.
"""

import hashlib
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from cirra.domain import CirraError, canonical

DIRECTORIES = ("audit-publication", "audit-waivers")
MAX_RECEIPT_BYTES = 65536
ROW_PAGE = 32
READ_SECONDS = 6
ROW_COLUMNS = (
    "attempt_id, number, state, report_digest, created_at, completed_at, scope, "
    "comparison_json, validator_output"
)


class Changed(Exception):
    """A receipt was replaced while its digest or response was being read."""


def directory_version(folder):
    try:
        stat = folder.stat()
    except FileNotFoundError:
        return None
    try:
        with (folder / ".revision").open("rb") as marker:
            revision = marker.read(33)
    except FileNotFoundError:
        revision = None  # Existing directories written by older releases.
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, revision


def read_receipt(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_RECEIPT_BYTES + 1)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise CirraError("receipt exceeds 64 KiB")
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise CirraError("receipt is not an object")
    return record, hashlib.sha256(raw).hexdigest()


class Snapshot:
    def __init__(self, root, connect):
        self.root, self.connect = root, connect
        self.lock = threading.Lock()
        self.database = self.index = None
        self.database_file = self.row_version = self.generation = None
        self.directories = {}
        self.stopping = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audit-read-index")
        self.build = None

    def close(self):
        self.stopping.set()
        with self.lock:
            for connection in (self.database, self.index):
                if connection is not None:
                    connection.close()
        self.executor.shutdown(wait=True)

    def database_identity(self):
        stat = (self.root / "cirra.sqlite3").stat()
        return stat.st_dev, stat.st_ino

    def connection(self):
        identity = self.database_identity()
        if self.database is None or self.database_file != identity:
            if self.database is not None:
                self.database.close()
                self.database = None
            self.database = self.connect()
            self.database_file, self.row_version = identity, None
        if self.index is None:
            # An unnamed SQLite file is private and automatically removed on close.
            # Keep only lookup metadata here, never private report or receipt bodies.
            self.index = sqlite3.connect("", check_same_thread=False)
            self.index.executescript(
                "CREATE TABLE receipts(folder, name, fingerprint, sha, audit_id, seen INTEGER,"
                " PRIMARY KEY(folder,name)) WITHOUT ROWID;"
                "CREATE INDEX by_audit ON receipts(folder,audit_id);"
            )
        return self.database

    def rows(self, connection):
        prefix = hashlib.sha256(b"[[")
        last = count = 0
        while True:
            if self.stopping.is_set():
                raise CirraError("audit reader is closed")
            page = connection.execute(
                f"SELECT rowid, {ROW_COLUMNS} FROM audits WHERE rowid>? ORDER BY rowid LIMIT ?",
                (last, ROW_PAGE),
            )
            used = 0
            try:
                for row in page:
                    if count:
                        prefix.update(b",")
                    prefix.update(canonical(list(row)[1:]).encode())
                    last = row[0]
                    used += 1
                    count += 1
            finally:
                page.close()
            if used < ROW_PAGE:
                break
        prefix.update(b"],[")
        return prefix, count

    def refresh_directory(self, folder, version):
        if version is None:
            self.index.execute("DELETE FROM receipts WHERE folder=?", (folder,))
            return
        self.index.execute("UPDATE receipts SET seen=0 WHERE folder=?", (folder,))
        with os.scandir(self.root / folder) as entries:
            for entry in entries:
                if self.stopping.is_set():
                    raise CirraError("audit reader is closed")
                if not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
                fingerprint = canonical(
                    [stat.st_dev, stat.st_ino, stat.st_ctime_ns, stat.st_mtime_ns, stat.st_size]
                )
                known = self.index.execute(
                    "UPDATE receipts SET seen=1 WHERE folder=? AND name=? AND fingerprint=?",
                    (folder, entry.name, fingerprint),
                )
                if known.rowcount:
                    continue
                record, sha = read_receipt(self.root / folder / entry.name)
                audit_id = record.get("audit_id")
                self.index.execute(
                    "INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?,1)",
                    (
                        folder,
                        entry.name,
                        fingerprint,
                        sha,
                        audit_id if isinstance(audit_id, str) else None,
                    ),
                )
        self.index.execute("DELETE FROM receipts WHERE folder=? AND seen=0", (folder,))

    def refresh(self, connection, version, directories):
        if self.row_version != version:
            self.prefix, self.count = self.rows(connection)
        changed = self.row_version != version or self.directories != directories
        if self.directories != directories:
            with self.index:
                for name, current in directories.items():
                    if name not in self.directories or self.directories[name] != current:
                        self.refresh_directory(name, current)
        if changed:
            result = self.prefix.copy()
            for i, row in enumerate(
                self.index.execute("SELECT folder,name,sha FROM receipts ORDER BY folder,name")
            ):
                if i:
                    result.update(b",")
                result.update(canonical(list(row)).encode())
            result.update(b"]]")
            self.generation = result.hexdigest()
        self.row_version, self.directories = version, directories

    def gate(self, audit_id, deadline):
        publication, waivers = None, []
        for folder, name, sha in self.index.execute(
            "SELECT folder,name,sha FROM receipts WHERE folder='audit-publication' AND name=? "
            "UNION ALL SELECT folder,name,sha FROM receipts "
            "WHERE folder='audit-waivers' AND audit_id=?",
            (audit_id + ".json", audit_id),
        ):
            if time.monotonic() >= deadline:
                raise CirraError("audit read deadline exceeded")
            record, actual = read_receipt(self.root / folder / name)
            if actual != sha:
                with self.index:
                    self.index.execute(
                        "DELETE FROM receipts WHERE folder=? AND name=?", (folder, name)
                    )
                raise Changed
            if folder == "audit-publication":
                publication = record
            else:
                waivers.append(record)
        if publication is None and not waivers:
            return 404, {"error": "not found"}
        waivers.sort(key=lambda r: (str(r.get("waived_at")), str(r.get("request_id"))))
        return 200, {"audit_id": audit_id, "publication": publication, "waivers": waivers}

    def versions(self, connection):
        return (
            connection.execute("PRAGMA data_version").fetchone()[0],
            {name: directory_version(self.root / name) for name in DIRECTORIES},
        )

    def rebuild(self):
        with self.lock:
            if self.stopping.is_set():
                raise CirraError("audit reader is closed")
            connection = self.connection()
            self.refresh(connection, *self.versions(connection))

    def read(self, expected_schema, query):
        deadline = time.monotonic() + READ_SECONDS
        for _ in range(6):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.lock.acquire(timeout=remaining):
                raise CirraError("audit read deadline exceeded")
            try:
                if self.stopping.is_set():
                    raise CirraError("audit reader is closed")
                connection = self.connection()
                version, directories = self.versions(connection)
                if connection.execute("PRAGMA user_version").fetchone()[0] != expected_schema:
                    return (
                        503,
                        {"error": "audit store schema is not served by this Cirra version"},
                        None,
                    )
                if (self.row_version, self.directories) == (version, directories):
                    connection.set_progress_handler(lambda: time.monotonic() >= deadline, 2000)
                    try:
                        status, record = query(connection, self, deadline)
                    except (Changed, FileNotFoundError):
                        self.directories = {}
                        continue
                    finally:
                        connection.set_progress_handler(None, 0)
                    if (version, directories) == self.versions(
                        connection
                    ) and self.database_file == self.database_identity():
                        return status, record, self.generation
                    continue
                if self.build is None or self.build.done():
                    self.build = self.executor.submit(self.rebuild)
                build = self.build
            finally:
                self.lock.release()
            try:
                build.result(timeout=max(0, deadline - time.monotonic()))
            except TimeoutError:
                # A large cold index continues warming for the next caller.
                raise CirraError("audit read index is warming") from None
        raise CirraError("audit store changed during the read")
