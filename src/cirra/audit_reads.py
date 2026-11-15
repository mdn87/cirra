"""Read-only loopback HTTP route over the audit store for derived views.

The listener binds 127.0.0.1 only, answers GET only, opens the SQLite store read-only,
holds no credentials and never writes. Every response carries the record, the record's
SHA-256 and the audit store's generation, so a reader can hash exactly what it saw and
refuse a store that changed while it was reading. Gate verdicts are the receipts Cirra
stored when it published or waived an audit; nothing is recomputed here. Audit packets are
not served: they hold repository content a derived view has no use for.
"""

import hashlib
import json
import re
import socket
import sqlite3
import sys
import threading
from contextlib import closing
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cirra import __version__
from cirra.domain import CirraError, canonical, digest
from cirra.store import SCHEMA_VERSION

PROTOCOL = "cirra.audit-reads.v1"
DEFAULT_PORT = 8797
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_RECEIPT_BYTES = 65536
ROUTES = ("/healthz", "/audits", "/audits/<attempt_id>", "/gates/<audit_id>")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX = re.compile(r"[0-9a-f]{32,64}\Z")
TOKEN = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
FILTERS = {"head": HEX, "repository_key": HEX64, "tier": TOKEN, "state": TOKEN}
SUMMARY = (
    "attempt_id",
    "audit_id",
    "number",
    "repository_key",
    "tier",
    "head",
    "state",
    "scope",
    "report_digest",
)
GENERATION_ROWS = (
    "SELECT attempt_id, number, state, report_digest, created_at, completed_at, scope, "
    "comparison_json, validator_output FROM audits ORDER BY rowid"
)
RECEIPT_DIRECTORIES = ("audit-publication", "audit-waivers")


def log_line(record):
    print(json.dumps(record, sort_keys=True), file=sys.stderr, flush=True)


def stamp(value):
    return None if value is None else datetime.fromtimestamp(value, UTC).isoformat()


def loads(value):
    return None if value is None else json.loads(value)


def summary(row):
    record = {name: row[name] for name in SUMMARY}
    record["created_at"] = stamp(row["created_at"])
    record["completed_at"] = stamp(row["completed_at"])
    record["sequence"] = row["rowid"]
    return record


def detail(row):
    record = summary(row)
    record["identity"] = loads(row["identity_json"])
    record["comparison"] = loads(row["comparison_json"])
    record["report"] = loads(row["report_json"])
    record["validator_output"] = row["validator_output"]
    return record


def route(path):
    """Map a request path to a route name and key; unknown paths map to (None, None)."""
    if path == "/healthz":
        return "healthz", None
    if path == "/audits":
        return "audits", None
    head, _, tail = path.rpartition("/")
    if head == "/audits" and HEX.match(tail):
        return "audit", tail
    if head == "/gates" and HEX64.match(tail):
        return "gate", tail
    return None, None


def parse_index_query(raw):
    """Validate the bounded page query of /audits; returns (limit, after, filters) or an error."""
    try:
        query = parse_qs(raw, keep_blank_values=True, strict_parsing=True) if raw else {}
    except ValueError:
        return None, "malformed query"
    if any(len(values) != 1 for values in query.values()) or set(query) - set(FILTERS) - {
        "limit",
        "after",
    }:
        return None, "unsupported query"
    values = {key: value[0] for key, value in query.items()}
    limit = values.pop("limit", str(DEFAULT_LIMIT))
    after = values.pop("after", "0")
    if not (limit.isdigit() and 1 <= int(limit) <= MAX_LIMIT):
        return None, f"limit must be an integer from 1 to {MAX_LIMIT}"
    if not (after.isdigit() and len(after) <= 18):
        return None, "after must be a sequence number"
    if any(not FILTERS[key].match(value) for key, value in values.items()):
        return None, "malformed filter"
    return (int(limit), int(after), values), None


class AuditReadServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    request_deadline = 10

    def __init__(self, address, state_dir):
        if address[0] != "127.0.0.1":
            raise CirraError("audit read listener must bind to 127.0.0.1")
        self.state_dir = Path(state_dir).resolve()
        self.store_path = self.state_dir / "cirra.sqlite3"
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, ReadHandler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        log_line({"event": "audit_read_error", "type": type(sys.exc_info()[1]).__name__})

    def connect(self):
        """Open the store read-only; a missing store or hot journal raises, never creates."""
        connection = sqlite3.connect(
            self.store_path.as_uri() + "?mode=ro", uri=True, timeout=10, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def read(self, raw_path):
        """Answer one GET as (status, body); the record and generation share one read."""
        parts = urlsplit(raw_path)
        name, key = route(parts.path)
        if name is None:
            return 404, {"error": "not found"}
        if parts.query and name != "audits":
            return 400, {"error": "query not accepted"}
        with closing(self.connect()) as connection:
            connection.execute("BEGIN")
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version != SCHEMA_VERSION:
                    return 503, {"error": "audit store schema is not served by this Cirra version"}
                generation = self.generation(connection)
                if name == "healthz":
                    status, record = 200, self.healthz(connection)
                elif name == "audits":
                    status, record = self.index(connection, parts.query)
                elif name == "audit":
                    status, record = self.audit(connection, key)
                else:
                    status, record = self.gate(key)
            finally:
                connection.execute("ROLLBACK")
        if status != 200:
            return status, record
        return 200, {
            "protocol": PROTOCOL,
            "schema_version": version,
            "generation": generation,
            "record": record,
            "sha256": digest(record),
        }

    def generation(self, connection):
        """Digest of every served audit row and receipt; stable while the served content is."""
        rows = [list(row) for row in connection.execute(GENERATION_ROWS)]
        receipts = []
        for name in RECEIPT_DIRECTORIES:
            folder = self.state_dir / name
            for path in sorted(folder.glob("*.json")) if folder.is_dir() else ():
                receipts.append([name, path.name, hashlib.sha256(path.read_bytes()).hexdigest()])
        return digest([rows, receipts])

    def healthz(self, connection):
        count = connection.execute("SELECT count(*) FROM audits").fetchone()[0]
        return {"status": "ok", "version": __version__, "audits": count}

    def index(self, connection, raw_query):
        parsed, error = parse_index_query(raw_query)
        if error:
            return 400, {"error": error}
        limit, after, filters = parsed
        clauses = ["rowid > ?"] + [f"{key} = ?" for key in sorted(filters)]
        params = [after] + [filters[key] for key in sorted(filters)] + [limit + 1]
        rows = connection.execute(
            f"SELECT rowid, * FROM audits WHERE {' AND '.join(clauses)} ORDER BY rowid LIMIT ?",
            params,
        ).fetchall()
        page = rows[:limit]
        more = len(rows) > len(page)
        return 200, {
            "audits": [summary(row) for row in page],
            "next": page[-1]["rowid"] if more else None,
        }

    def audit(self, connection, attempt_id):
        row = connection.execute(
            "SELECT rowid, * FROM audits WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            return 404, {"error": "not found"}
        return 200, detail(row)

    def gate(self, audit_id):
        """The gate as Cirra stored it: the publication receipt and any release waivers."""
        publication = self.receipt(self.state_dir / "audit-publication" / f"{audit_id}.json")
        waivers = [
            receipt
            for receipt in self.receipts("audit-waivers")
            if receipt.get("audit_id") == audit_id
        ]
        if publication is None and not waivers:
            return 404, {"error": "not found"}
        waivers.sort(key=lambda r: (str(r.get("waived_at")), str(r.get("request_id"))))
        return 200, {"audit_id": audit_id, "publication": publication, "waivers": waivers}

    def receipts(self, name):
        folder = self.state_dir / name
        if not folder.is_dir():
            return []
        return [self.receipt(path) for path in sorted(folder.glob("*.json"))]

    def receipt(self, path):
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_RECEIPT_BYTES:
            raise CirraError("receipt exceeds 64 KiB")
        record = json.loads(path.read_bytes())
        if not isinstance(record, dict):
            raise CirraError("receipt is not an object")
        return record


class ReadHandler(BaseHTTPRequestHandler):
    server_version = "Cirra"
    sys_version = ""

    def setup(self):
        self.request.settimeout(5)
        self.deadline = threading.Timer(self.server.request_deadline, self.expire)
        self.deadline.daemon = True
        self.deadline.start()
        super().setup()

    def expire(self):
        try:
            self.request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        self.deadline.cancel()
        try:
            super().finish()
        except OSError:
            pass

    def log_message(self, format, *args):
        pass

    def log_request(self, code="-", size="-"):
        name, _ = route(urlsplit(self.path).path) if self.command == "GET" else (None, None)
        log_line(
            {
                "event": "audit_read",
                "method": self.command,
                "route": name or "(other)",
                "status": int(code) if str(code).isdigit() else None,
            }
        )

    def __getattr__(self, name):
        # Every method other than GET is answered 405, whatever its name.
        if name.startswith("do_"):
            return self.method_not_allowed
        raise AttributeError(name)

    def method_not_allowed(self):
        self.respond(405, {"error": "method not allowed", "allow": ["GET"]}, allow=True)

    def do_GET(self):
        try:
            status, body = self.server.read(self.path)
        except (CirraError, OSError, ValueError, sqlite3.Error) as exc:
            log_line({"event": "audit_read_error", "type": type(exc).__name__})
            status, body = 503, {"error": "audit store unavailable"}
        self.respond(status, body)

    def respond(self, status, body, *, allow=False):
        payload = canonical(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if allow:
            self.send_header("Allow", "GET")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True
