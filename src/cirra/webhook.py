"""A bounded loopback HTTP receiver. A TLS reverse proxy owns public ingress."""

import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cirra import events
from cirra.domain import CirraError, DeliveryConflict, canonical, utc_now
from cirra.job_queue import Queue
from cirra.locking import change_lock
from cirra.queue_format import activate
from cirra.reviewer import unique_object

MAX_BODY = 1024 * 1024


def accept(route, raw, headers, secret, queue, on_eligible=None):
    _, kind = events.authenticate(route.provider, raw, headers, secret)
    if kind == "ping" and route.provider == "github":
        return {"state": "ignored", "reason": "ping"}
    if kind != ("pull_request" if route.provider == "github" else "Merge Request Hook"):
        return {"state": "ignored", "reason": "event type"}
    hint = events.event_hint(route.provider, route.instance, raw, headers, secret, route.policy)
    if hint["repository_id"] != route.repository_id or hint["repository"] != route.repository:
        raise CirraError("webhook repository does not match this configured route")
    payload = json.loads(raw, object_pairs_hook=unique_object)
    if route.provider == "github":
        action = payload.get("action")
        actions = {"opened", "reopened", "synchronize", "ready_for_review", "edited"}
    else:
        action = payload["object_attributes"].get("action")
        actions = {"open", "reopen", "update"}
    if action not in actions:
        return {"state": "ignored", "reason": "event action"}
    if not trusted_author(route.provider, payload):
        return {"state": "ignored", "reason": "untrusted author"}
    if metadata_only(route.provider, action, payload):
        return {"state": "ignored", "reason": "metadata-only edit"}
    if on_eligible:
        on_eligible()
    return {"state": "accepted", **queue.enqueue(route, hint)}


def trusted_author(provider, payload):
    change = payload["pull_request"] if provider == "github" else payload["object_attributes"]
    return events.trusted_change(provider, change)


def metadata_only(provider, action, payload):
    """Title, body, or label edits that change neither the head nor the base."""
    if provider == "github":
        return action == "edited" and "base" not in (payload.get("changes") or {})
    return (
        action == "update"
        and "oldrev" not in payload["object_attributes"]
        and "target_branch" not in (payload.get("changes") or {})
    )


DELIVERY_HEADERS = ("X-GitHub-Delivery", "X-Gitlab-Event-UUID", "X-Forgejo-Delivery")


def log_line(record):
    print(
        json.dumps({"observed_at": utc_now(), **record}, sort_keys=True),
        file=sys.stderr,
        flush=True,
    )


class WebhookServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    # Total time a client may take to send its request; per-read timeouts alone
    # let a slow client hold one of the 16 slots indefinitely.
    request_deadline = 10

    def __init__(self, address, config, directory, credential):
        from cirra.coordination import connect

        if address[0] != "127.0.0.1":
            raise CirraError("webhook listener must bind to 127.0.0.1 behind an HTTPS proxy")
        self.config, self.queue = config, Queue(directory, config.max_jobs)
        self.shared_inbox = connect(config, directory, credential)
        self.selector, self.credential = Path(directory).absolute(), credential
        self.selection_lock = threading.Lock()
        self.secrets = {name: credential(route.secret_env) for name, route in config.routes.items()}
        if any(not isinstance(secret, str) or len(secret) < 32 for secret in self.secrets.values()):
            raise CirraError("service webhook secrets must contain at least 32 characters")
        self.slots = threading.BoundedSemaphore(16)
        activate(self.queue.directory.parent)
        super().__init__(address, Handler)

    def intake(self):
        from cirra.coordination import connect

        if self.shared_inbox is None:
            return self.queue
        with self.selection_lock:
            selected = self.selector.resolve(strict=True)
            if selected != self.shared_inbox.queue.directory.parent:
                self.shared_inbox = connect(self.config, selected, self.credential)
            return self.shared_inbox

    def enqueue(self, route, hint):
        return self.intake().enqueue(route, hint)

    def check_local_admission(self):
        """Probe local storage and the bounded slot census without admitting a job."""
        queue = self.queue
        with change_lock(queue.directory / "locks", "enqueue", 0.2):
            count = queue.slots.count()
            if count is None or not 0 <= count < queue.maximum:
                raise CirraError("local queue admission is unavailable")
            # Admission writes the root's slot counter, a queue job and a delivery
            # receipt. Probe their actual directories, not os.access permissions.
            directories = [queue.directory.parent, queue.directory]
            deliveries = queue.directory / "deliveries"
            if deliveries.exists():
                directories.append(deliveries)
            for directory in directories:
                with tempfile.TemporaryFile(dir=directory, prefix=".cirra-health-") as stream:
                    stream.write(b"1")
                    stream.flush()
                    os.fsync(stream.fileno())

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
        # Never echo payloads or credential-bearing headers in a traceback; the
        # exception type alone shows that something failed and what kind.
        log_line(
            {
                "event": "webhook_error",
                "level": "error",
                "operation": "accept_connection",
                "phase": "connection",
                "request_id": uuid.uuid4().hex,
                "type": type(sys.exc_info()[1]).__name__,
            }
        )


class Handler(BaseHTTPRequestHandler):
    server_version = "Cirra"
    sys_version = ""

    def setup(self):
        self.request_id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.job_id = None
        self.admission = None
        self.response_status = None
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
        if self.response_status is not None or self.admission is not None:
            log_line(
                {
                    "event": "webhook_request",
                    **self.request_context(),
                    "status": self.response_status,
                }
            )

    def log_message(self, format, *args):
        pass  # Free-form messages can contain the raw request line.

    def request_context(self):
        path = getattr(self, "path", "")
        headers = getattr(self, "headers", {})
        known = any(path == "/hooks/" + name for name in self.server.config.routes)
        delivery = next((headers.get(name) for name in DELIVERY_HEADERS if headers.get(name)), None)
        try:
            size = int(headers.get("Content-Length", "0"))
        except ValueError:
            size = None
        return {
            "request_id": self.request_id,
            "job_id": self.job_id,
            "admission": self.admission,
            "method": self.command
            if getattr(self, "command", None) in ("GET", "POST")
            else "(other)",
            "path": path if known or path == "/healthz" else "(other)",
            "duration_ms": round((time.monotonic() - self.started) * 1000, 2),
            "delivery": delivery
            if delivery and re.fullmatch(r"[A-Za-z0-9-]{1,64}", delivery)
            else None,
            "bytes": size,
        }

    def log_request(self, code="-", size="-"):
        self.response_status = int(code) if str(code).isdigit() else None

    def record_eligible(self):
        # The journal can still account for an eligible request when storage is down.
        self.admission = "refused"

    def handle(self):
        try:
            super().handle()
        except Exception as exc:
            log_line(
                {
                    "event": "webhook_error",
                    "level": "error",
                    "operation": "handle_request",
                    **self.request_context(),
                    "type": type(exc).__name__,
                }
            )
            if self.response_status is None and hasattr(self, "request_version"):
                self.respond(500, {"error": "receiver failed", "request_id": self.request_id})

    def respond(self, status, result):
        body = canonical(result).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        if self.path == "/healthz":
            try:
                if self.server.shared_inbox is not None:
                    self.server.intake().check()
                else:
                    self.server.check_local_admission()
            except (CirraError, OSError):
                return self.respond(503, {"status": "admission unavailable"})
        self.respond(
            200 if self.path == "/healthz" else 404,
            {"status": "ok"} if self.path == "/healthz" else {"error": "not found"},
        )

    def do_POST(self):
        route = next(
            (
                route
                for name, route in self.server.config.routes.items()
                if self.path == "/hooks/" + name
            ),
            None,
        )
        if route is None:
            return self.respond(404, {"error": "not found"})
        keys = [key.lower() for key in self.headers.keys()]
        if len(keys) != len(set(keys)) or "transfer-encoding" in keys:
            return self.respond(400, {"error": "duplicate or unsupported framing headers"})
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if not 1 <= length <= MAX_BODY:
            return self.respond(413, {"error": "body length must be between one byte and 1 MiB"})
        if self.headers.get_content_type() != "application/json":
            return self.respond(415, {"error": "application/json required"})
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                return self.respond(400, {"error": "incomplete body"})
            result = accept(
                route,
                raw,
                dict(self.headers),
                self.server.secrets[route.name],
                self.server,
                self.record_eligible,
            )
        except DeliveryConflict:
            self.admission = None
            self.respond(400, {"error": "delivery identity was reused with a different payload"})
        except CirraError as exc:
            code = 503 if self.admission == "refused" else 400
            if "authentication" in str(exc):
                code = 401
            self.respond(
                code, {"error": "delivery refused; check authentication, route, or capacity"}
            )
        except (TimeoutError, ConnectionError, ValueError, RecursionError):
            if self.admission == "refused":
                self.respond(
                    503, {"error": "delivery storage unavailable", "request_id": self.request_id}
                )
            else:
                self.respond(400, {"error": "invalid or interrupted delivery"})
        except OSError as exc:
            log_line(
                {
                    "event": "webhook_error",
                    "level": "error",
                    "operation": "enqueue_delivery",
                    **self.request_context(),
                    "type": type(exc).__name__,
                    "phase": "persist_delivery",
                }
            )
            self.respond(
                503, {"error": "delivery storage unavailable", "request_id": self.request_id}
            )
        else:
            self.job_id = result.get("job_id")
            if self.job_id:
                self.admission = "duplicate" if result["duplicate"] else "accepted"
            self.respond(202, result)
