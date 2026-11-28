"""Read-only loopback HTTP route over the audit store for derived views.

The listener binds 127.0.0.1 only, answers GET only, opens the SQLite store read-only,
and requires a dedicated bearer credential except for /healthz. It has no publisher
credentials and never writes application state. Every success carries the record, its
SHA-256 and the audit store's generation, so a reader can hash exactly what it saw and
refuse a store that changed while it was reading. Gate verdicts are the receipts Cirra
stored when it published or waived an audit; nothing is recomputed here. Audit packets are
not served: they hold repository content a derived view has no use for.
"""

import hashlib
import hmac
import json
import os
import re
import socket
import sqlite3
import stat
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cirra import __version__
from cirra.audit_read_snapshot import Snapshot
from cirra.domain import CirraError, canonical, digest
from cirra.store import SCHEMA_VERSION

PROTOCOL = "cirra.audit-reads.v1"
DEFAULT_PORT = 8797
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
ROUTES = ("/healthz", "/audits", "/audits/<attempt_id>", "/gates/<audit_id>")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX = re.compile(r"[0-9a-f]{32,64}\Z")
TOKEN = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
FILTERS = {"head": HEX, "repository_key": HEX64, "tier": TOKEN, "state": TOKEN}
BEARER_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]{32,256}={0,2}\Z")
MAX_TOKEN_BYTES = 256
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
SUMMARY_COLUMNS = ", ".join(("rowid", *SUMMARY, "created_at", "completed_at"))


def log_line(record):
    print(json.dumps(record, sort_keys=True), file=sys.stderr, flush=True)


def stamp(value):
    return None if value is None else datetime.fromtimestamp(value, UTC).isoformat()


def loads(value):
    return None if value is None else json.loads(value)


def token_digest(token=None):
    """Load one local read credential at startup; never include its value or path in errors."""
    try:
        if token is None:
            token = os.environ.get("CIRRA_AUDIT_READ_TOKEN")
            filename = os.environ.get("CIRRA_AUDIT_READ_TOKEN_FILE")
            if token is not None and filename is not None:
                raise ValueError("ambiguous credential")
            if filename is not None:
                path = Path(filename)
                if not filename or path.is_symlink():
                    raise ValueError("credential must be a regular file")
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                with os.fdopen(os.open(path, flags), "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError("credential must be a regular file")
                    if os.name == "posix" and (
                        info.st_uid not in (0, os.getuid()) or info.st_mode & 0o137
                    ):
                        raise ValueError("credential permissions are not private")
                    raw = stream.read(MAX_TOKEN_BYTES + 3)
                if raw.endswith(b"\r\n"):
                    raw = raw[:-2]
                elif raw.endswith(b"\n"):
                    raw = raw[:-1]
                token = raw.decode("ascii")
        if (
            not isinstance(token, str)
            or len(token) > MAX_TOKEN_BYTES
            or not BEARER_TOKEN.fullmatch(token)
        ):
            raise ValueError("credential is missing or malformed")
        return hashlib.sha256(token.encode("ascii")).digest()
    except (OSError, UnicodeError, ValueError):
        raise CirraError(
            "audit reader credential unavailable: configure one valid "
            "CIRRA_AUDIT_READ_TOKEN or CIRRA_AUDIT_READ_TOKEN_FILE"
        ) from None


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
        if len(raw) > 1024:
            return None, "query exceeds length limit"
        query = parse_qs(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=6)
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
    if not (re.fullmatch(r"[0-9]{1,18}", limit) and 1 <= int(limit) <= MAX_LIMIT):
        return None, f"limit must be an integer from 1 to {MAX_LIMIT}"
    if not re.fullmatch(r"[0-9]{1,18}", after):
        return None, "after must be a sequence number"
    if any(not FILTERS[key].match(value) for key, value in values.items()):
        return None, "malformed filter"
    return (int(limit), int(after), values), None


class AuditReadServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    request_deadline = 10

    def __init__(self, address, state_dir, *, token=None):
        if address[0] != "127.0.0.1":
            raise CirraError("audit read listener must bind to 127.0.0.1")
        self._token_digest = token_digest(token)
        self.state_dir = Path(state_dir).resolve()
        self.state_selector = os.environ.get("CIRRA_AUDIT_STATE_SELECTOR")
        self.store_path = self.state_dir / "cirra.sqlite3"
        self.slots = threading.BoundedSemaphore(16)
        self.snapshot = Snapshot(self.state_dir, self.connect)
        self.request_context = threading.local()
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
        context = self.request_context
        started = time.monotonic()
        context.id = uuid.uuid4().hex
        context.route = context.method = "(other)"
        context.status = None
        context.outcome = "disconnected"
        try:
            super().process_request_thread(request, client_address)
        finally:
            try:
                level = "info"
                if context.outcome == "failed" or (context.status or 0) >= 500:
                    level = "error"
                elif context.outcome == "disconnected":
                    level = "warning"
                log_line(
                    {
                        "event": "audit_read",
                        "level": level,
                        "operation": "audit_read",
                        "method": context.method,
                        "route": context.route,
                        "request_id": context.id,
                        "duration_ms": round(1000 * (time.monotonic() - started), 2),
                        "status": context.status,
                        "outcome": context.outcome,
                    }
                )
            finally:
                self.slots.release()

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError) and getattr(self.request_context, "id", None):
            self.request_context.outcome = "disconnected"
            return
        self.request_context.outcome = "failed"
        self.log_error(exc)

    def log_error(self, exc):
        log_line(
            {
                "event": "audit_read_error",
                "level": "error",
                "operation": "audit_read",
                "request_id": getattr(self.request_context, "id", None) or uuid.uuid4().hex,
                "route": getattr(self.request_context, "route", "(other)"),
                "type": type(exc).__name__,
            }
        )

    def server_close(self):
        super().server_close()
        self.snapshot.close()

    def connect(self):
        """Open the store read-only; a missing store or hot journal raises, never creates."""
        connection = sqlite3.connect(
            self.store_path.as_uri() + "?mode=ro",
            uri=True,
            timeout=2,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def read(self, raw_path):
        """Trusted in-process read; HTTP callers are authorized by ReadHandler first."""
        parts = urlsplit(raw_path)
        name, key = route(parts.path)
        if name is None:
            return 404, {"error": "not found"}
        if parts.query and name != "audits":
            return 400, {"error": "query not accepted"}
        parsed = None
        if name == "audits":
            parsed, error = parse_index_query(parts.query)
            if error:
                return 400, {"error": error}

        def query(connection, snapshot, deadline):
            if name == "healthz":
                return 200, {"status": "ok", "version": __version__, "audits": snapshot.count}
            if name == "audits":
                return self.index(connection, parsed)
            if name == "audit":
                return self.audit(connection, key)
            return snapshot.gate(key, deadline)

        self.check_selected_state()
        status, record, generation = self.snapshot.read(SCHEMA_VERSION, query)
        self.check_selected_state()
        if status != 200:
            return status, record
        return 200, {
            "protocol": PROTOCOL,
            "schema_version": SCHEMA_VERSION,
            "generation": generation,
            "record": record,
            "sha256": digest(record),
        }

    def check_selected_state(self):
        # A read-only bind mount retains its original inode after the host switches
        # the managed symlink. Withhold records until systemd rebinds the new root.
        if self.state_selector and not self.state_dir.samefile(self.state_selector):
            raise CirraError("the current audit state changed")

    def index(self, connection, parsed):
        limit, after, filters = parsed
        clauses = ["rowid > ?"] + [f"{key} = ?" for key in sorted(filters)]
        params = [after] + [filters[key] for key in sorted(filters)] + [limit + 1]
        rows = connection.execute(
            f"SELECT {SUMMARY_COLUMNS} FROM audits WHERE {' AND '.join(clauses)} "
            "ORDER BY rowid LIMIT ?",
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
        try:
            super().finish()
        except OSError:
            self.server.request_context.outcome = "disconnected"
        finally:
            self.deadline.cancel()

    def log_message(self, format, *args):
        pass

    def log_request(self, code="-", size="-"):
        context = self.server.request_context
        context.status = int(code) if str(code).isdigit() else None
        method = getattr(self, "command", None)
        if method in ("GET", "HEAD", "PUT", "POST", "OPTIONS", "DELETE", "PATCH"):
            context.method = method

    def send_error(self, code, message=None, explain=None):
        super().send_error(code, message, explain)
        self.response_written()

    def response_written(self):
        self.wfile.flush()
        self.server.request_context.outcome = "completed"

    def __getattr__(self, name):
        # Every method other than GET is answered 405, whatever its name.
        if name.startswith("do_"):
            return self.method_not_allowed
        raise AttributeError(name)

    def method_not_allowed(self):
        self.respond(405, {"error": "method not allowed", "allow": ["GET"]}, allow=True)

    def do_GET(self):
        try:
            self.server.request_context.route = route(urlsplit(self.path).path)[0] or "(other)"
            if urlsplit(self.path).path != "/healthz" and not self.authorized():
                self.respond(401, {"error": "unauthorized"})
                return
            status, body = self.server.read(self.path)
        except (CirraError, OSError, ValueError, RecursionError, sqlite3.Error) as exc:
            self.server.log_error(exc)
            status, body = 503, {"error": "audit store unavailable"}
        self.respond(status, body)

    def authorized(self):
        values = self.headers.get_all("Authorization", [])
        if len(values) != 1:
            return False
        scheme, _, token = values[0].partition(" ")
        if (
            scheme.lower() != "bearer"
            or len(token) > MAX_TOKEN_BYTES
            or not BEARER_TOKEN.fullmatch(token)
        ):
            return False
        return hmac.compare_digest(
            hashlib.sha256(token.encode("ascii")).digest(), self.server._token_digest
        )

    def respond(self, status, body, *, allow=False):
        payload = canonical(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if status == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="cirra-audit-reads"')
        if allow:
            self.send_header("Allow", "GET")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.response_written()
        self.close_connection = True

    def end_headers(self):
        self.send_header("X-Request-ID", self.server.request_context.id)
        super().end_headers()
