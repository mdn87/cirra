"""One local SQLite store, with expiring per-change claims and fenced writes."""

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cirra.domain import CirraError, canonical
from cirra.locking import change_lock


class LostClaim(CirraError):
    pass


@dataclass(frozen=True)
class Claim:
    change_key: str
    owner: str
    generation: int


SCHEMA = [
    """CREATE TABLE claims (
        change_key TEXT PRIMARY KEY, owner TEXT NOT NULL,
        generation INTEGER NOT NULL, expires_at REAL NOT NULL)""",
    """CREATE TABLE attempts (
        run_id TEXT PRIMARY KEY, change_key TEXT NOT NULL, analysis_digest TEXT NOT NULL,
        number INTEGER NOT NULL, state TEXT NOT NULL, input_json TEXT NOT NULL,
        response_json TEXT, result_json TEXT, duration_seconds REAL,
        created_at REAL NOT NULL, UNIQUE(analysis_digest, number))""",
    """CREATE TABLE reports (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
        report_digest TEXT NOT NULL UNIQUE, body TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES attempts(run_id))""",
]

# SCHEMA creates version 1. MIGRATIONS[n] holds the statements that take a
# version-n database to n + 1; append an entry and bump SCHEMA_VERSION for
# every schema change so existing state (reviews, attempts) carries over.
SCHEMA_VERSION = 1
MIGRATIONS = {}


class Store:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "cirra.sqlite3"
        with self.transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchone():
                    raise CirraError(
                        "state database has an unknown schema; choose a new state directory"
                    )
                for statement in SCHEMA:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version=1")
                version = 1
            if version > SCHEMA_VERSION:
                raise CirraError(
                    "state schema is newer than this Cirra version; upgrade Cirra or roll back"
                )
            while version < SCHEMA_VERSION:
                if version not in MIGRATIONS:
                    raise CirraError("unsupported state schema; use a compatible Cirra version")
                for statement in MIGRATIONS[version]:
                    connection.execute(statement)
                version += 1
                connection.execute(f"PRAGMA user_version={version}")

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def acquire(self, change_key, ttl):
        with self.transaction() as connection:
            now = time.time()
            previous = connection.execute(
                "SELECT * FROM claims WHERE change_key=?",
                (change_key,),
            ).fetchone()
            if previous and previous["expires_at"] > now:
                return None
            claim = Claim(
                change_key, uuid.uuid4().hex, previous["generation"] + 1 if previous else 1
            )
            connection.execute(
                "INSERT OR REPLACE INTO claims VALUES (?, ?, ?, ?)",
                (change_key, claim.owner, claim.generation, now + ttl),
            )
            return claim

    @staticmethod
    def _fence(connection, claim):
        current = connection.execute(
            "SELECT * FROM claims WHERE change_key=?",
            (claim.change_key,),
        ).fetchone()
        if (
            not current
            or current["owner"] != claim.owner
            or current["generation"] != claim.generation
            or current["expires_at"] <= time.time()
        ):
            raise LostClaim(
                "work claim expired or was superseded; retry using the same state directory"
            )

    def heartbeat(self, claim, ttl):
        with self.transaction() as connection:
            self._fence(connection, claim)
            connection.execute(
                "UPDATE claims SET expires_at=? WHERE change_key=?",
                (time.time() + ttl, claim.change_key),
            )

    def release(self, claim):
        with self.transaction() as connection:
            connection.execute(
                "UPDATE claims SET expires_at=0 WHERE change_key=? AND owner=? AND generation=?",
                (claim.change_key, claim.owner, claim.generation),
            )

    @contextmanager
    def lease(self, change_key, ttl, wait_seconds):
        # The kernel releases this lock on process exit. Remote calls hold only
        # this change's lock, never a database-wide transaction. SQL generations
        # still fence checkpoints, while other changes can keep heartbeating.
        with change_lock(self.path.parent / "locks", change_key, wait_seconds):
            with self._lease(change_key, ttl, wait_seconds) as claim:
                yield claim

    @contextmanager
    def _lease(self, change_key, ttl, wait_seconds):
        deadline = time.monotonic() + wait_seconds
        while (claim := self.acquire(change_key, ttl)) is None:
            if time.monotonic() >= deadline:
                raise CirraError(
                    "another invocation owns this change; retry using the same state directory"
                )
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        stopped = threading.Event()

        def renew():
            while not stopped.wait(ttl / 3):
                try:
                    self.heartbeat(claim, ttl)
                except sqlite3.OperationalError:
                    continue
                except (CirraError, sqlite3.Error):
                    return  # Every later write still has to pass the transaction's fence.

        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        try:
            yield claim
        finally:
            stopped.set()
            thread.join(timeout=11)
            self.release(claim)

    def latest_attempt_order(self, claim, run_id):
        with self.transaction() as connection:
            self._fence(connection, claim)
            newest = connection.execute(
                "SELECT run_id, rowid FROM attempts WHERE change_key=? ORDER BY rowid DESC LIMIT 1",
                (claim.change_key,),
            ).fetchone()
            if not newest or newest["run_id"] != run_id:
                raise CirraError("a newer review attempt supersedes this report")
            return newest["rowid"]

    def attempt(self, claim, analysis_digest, identity, rereview=False):
        with self.transaction() as connection:
            self._fence(connection, claim)
            previous = connection.execute(
                "SELECT * FROM attempts WHERE analysis_digest=? AND change_key=? "
                "ORDER BY number DESC LIMIT 1",
                (analysis_digest, claim.change_key),
            ).fetchone()
            if previous and not rereview:
                return dict(previous), True
            run_id = uuid.uuid4().hex
            number = previous["number"] + 1 if previous else 1
            connection.execute(
                """INSERT INTO attempts
                (run_id, change_key, analysis_digest, number, state, input_json, created_at)
                VALUES (?, ?, ?, ?, 'prepared', ?, ?)""",
                (
                    run_id,
                    claim.change_key,
                    analysis_digest,
                    number,
                    canonical(identity),
                    time.time(),
                ),
            )
            return dict(
                connection.execute("SELECT * FROM attempts WHERE run_id=?", (run_id,)).fetchone()
            ), False

    def _transition(self, claim, run_id, expected, state, **updates):
        with self.transaction() as connection:
            self._fence(connection, claim)
            attempt = connection.execute(
                "SELECT * FROM attempts WHERE run_id=? AND change_key=?",
                (run_id, claim.change_key),
            ).fetchone()
            if not attempt or attempt["state"] != expected:
                raise CirraError("review checkpoint does not permit this transition")
            # Only internal callers supply these column names; values remain bound parameters.
            assignments = ", ".join(f"{key}=?" for key in updates)
            suffix = f", {assignments}" if assignments else ""
            connection.execute(
                f"UPDATE attempts SET state=?{suffix} WHERE run_id=?",
                (state, *updates.values(), run_id),
            )

    def begin_call(self, claim, run_id):
        self._transition(claim, run_id, "prepared", "calling")

    def get_attempt(self, claim, run_id, analysis_digest):
        with self.transaction() as connection:
            self._fence(connection, claim)
            row = connection.execute(
                "SELECT * FROM attempts WHERE run_id=? AND change_key=?",
                (run_id, claim.change_key),
            ).fetchone()
            if not row or row["analysis_digest"] != analysis_digest:
                raise CirraError("run does not match the supplied snapshot or policy")
            latest = connection.execute(
                "SELECT MAX(number) FROM attempts WHERE analysis_digest=? AND change_key=?",
                (analysis_digest, claim.change_key),
            ).fetchone()[0]
            if row["number"] != latest:
                raise CirraError("review attempt was superseded by an explicit rereview")
            return dict(row)

    def record_session_response(self, claim, run_id, envelope):
        # No external call to recover: accept the complete handoff in one transaction.
        self._transition(claim, run_id, "prepared", "responded", response_json=canonical(envelope))

    def record_response(self, claim, run_id, envelope, duration):
        self._transition(
            claim,
            run_id,
            "calling",
            "responded",
            response_json=canonical(envelope),
            duration_seconds=duration,
        )

    def finish(self, claim, run_id, result):
        self._transition(claim, run_id, "responded", "complete", result_json=canonical(result))

    def mark_uncertain(self, claim, run_id, result):
        self._transition(claim, run_id, "calling", "uncertain", result_json=canonical(result))

    def save_report(self, claim, report):
        with self.transaction() as connection:
            self._fence(connection, claim)
            if not connection.execute(
                "SELECT 1 FROM attempts WHERE run_id=? AND change_key=?",
                (report["run_id"], claim.change_key),
            ).fetchone():
                raise CirraError("report does not belong to the claimed change")
            connection.execute(
                "INSERT OR IGNORE INTO reports(run_id, report_digest, body) VALUES (?, ?, ?)",
                (report["run_id"], report["report_digest"], canonical(report)),
            )

    def latest_report(self, run_id):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT body FROM reports WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            raise CirraError(
                "no saved report for that run; resume review with the original fixture and policy"
            )
        return json.loads(row["body"])
