"""Optional v3 audit invalidation counter. Only explicit maintenance changes the store."""

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from cirra.domain import CirraError

TABLE = "cirra_audit_generation_v1"
# These are the columns exposed by the v1 summary and detail routes. rowid is
# their public sequence. An UPDATE that assigns the existing values is a no-op.
AUDIT_FIELDS = (
    "state",
    "completed_at",
    "report_digest",
    "number",
    "scope",
    "validator_output",
    "comparison_json",
    "rowid",
    "attempt_id",
    "audit_id",
    "repository_key",
    "tier",
    "head",
    "created_at",
    "identity_json",
    "report_json",
)
UPDATE_WHEN = " OR ".join(f"OLD.{field} IS NOT NEW.{field}" for field in AUDIT_FIELDS)
DEFINITIONS = {
    TABLE: f"CREATE TABLE {TABLE} (id INTEGER PRIMARY KEY CHECK (id=1), "
    "revision_id TEXT NOT NULL CHECK (length(revision_id)=32), "
    "generation INTEGER NOT NULL CHECK (typeof(generation)='integer' AND generation>=0))"
}
for event in ("insert", "update", "delete"):
    name = f"{TABLE}_{event}"
    when = f" WHEN {UPDATE_WHEN}" if event == "update" else ""
    DEFINITIONS[name] = (
        f"CREATE TRIGGER {name} AFTER {event.upper()} ON audits{when} BEGIN "
        f"UPDATE {TABLE} SET generation=generation+1, revision_id=lower(hex(randomblob(16))) "
        "WHERE id=1; END"
    )


def definitions(database):
    names = tuple(DEFINITIONS)
    marks = ",".join("?" for _ in names)
    return dict(
        database.execute(
            f"SELECT name, sql FROM sqlite_schema WHERE name COLLATE NOCASE IN ({marks})", names
        )
    )


def counter(database):
    # Two restored copies can each reach the same count with different audit contents.
    row = database.execute(f"SELECT revision_id, generation FROM {TABLE} WHERE id=1").fetchone()
    if row and isinstance(row[0], str) and len(row[0]) == 32 and type(row[1]) is int:
        return tuple(row)
    return None


class RowRevision:
    def __init__(self):
        self.schema_cookie = None
        self.installed = False

    def read(self, database):
        # A concurrent installation or removal must present one complete schema.
        # End this read transaction before paging rows or walking receipt files.
        with database:
            database.execute("BEGIN")
            cookie = database.execute("PRAGMA schema_version").fetchone()[0]
            if cookie != self.schema_cookie:
                self.installed = definitions(database) == DEFINITIONS
                self.schema_cookie = cookie
            if self.installed and (revision := counter(database)) is not None:
                return ("audit", *revision)
            return ("sqlite", database.execute("PRAGMA data_version").fetchone()[0])


def maintain(state_dir, action):
    if action not in ("status", "install", "remove"):
        raise CirraError("unknown audit generation operation")
    mode = "ro" if action == "status" else "rw"
    path = (Path(state_dir) / "cirra.sqlite3").resolve().as_uri()
    with closing(sqlite3.connect(f"{path}?mode={mode}", uri=True, timeout=10)) as database:
        with database:
            database.execute("BEGIN" if action == "status" else "BEGIN IMMEDIATE")
            if database.execute("PRAGMA user_version").fetchone()[0] != 3:
                raise CirraError("audit generation maintenance requires an existing v3 store")
            current = definitions(database)
            if current and (current != DEFINITIONS or counter(database) is None):
                raise CirraError("audit generation metadata is incomplete or incompatible")
            if action == "install" and not current:
                fields = {row[1] for row in database.execute("PRAGMA table_info(audits)")}
                if set(AUDIT_FIELDS) - {"rowid"} != fields:
                    raise CirraError("audit generation requires the schema 3 audit table")
                for statement in DEFINITIONS.values():
                    database.execute(statement)
                database.execute(
                    f"INSERT INTO {TABLE} (id,revision_id,generation) VALUES (1,?,0)",
                    (uuid.uuid4().hex,),
                )
            if action == "remove" and current:
                for name in DEFINITIONS:
                    if name != TABLE:
                        database.execute(f"DROP TRIGGER {name}")
                database.execute(f"DROP TABLE {TABLE}")
            installed = action == "install" or (action == "status" and bool(current))
    return {"schema_version": 3, "audit_generation_installed": installed}
