"""A bounded loopback HTTP receiver. A TLS reverse proxy owns public ingress."""

import json
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cirra import events
from cirra.domain import CirraError, canonical
from cirra.job_queue import Queue
from cirra.reviewer import unique_object

MAX_BODY = 1024 * 1024


def accept(route, raw, headers, secret, queue):
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
    print(json.dumps(record, sort_keys=True), file=sys.stderr, flush=True)


class WebhookServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    # Total time a client may take to send its request; per-read timeouts alone
    # let a slow client hold one of the 16 slots indefinitely.
    request_deadline = 10

    def __init__(self, address, config, directory, credential):
        if address[0] != "127.0.0.1":
            raise CirraError("webhook listener must bind to 127.0.0.1 behind an HTTPS proxy")
        self.config, self.queue = config, Queue(directory, config.max_jobs)
        self.secrets = {name: credential(route.secret_env) for name, route in config.routes.items()}
        if any(not isinstance(secret, str) or len(secret) < 32 for secret in self.secrets.values()):
            raise CirraError("service webhook secrets must contain at least 32 characters")
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

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
        log_line({"event": "webhook_error", "type": type(sys.exc_info()[1]).__name__})


class Handler(BaseHTTPRequestHandler):
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
        pass  # Free-form messages can contain the raw request line.

    def log_request(self, code="-", size="-"):
        # One structured line per response: route path, status, delivery id and
        # declared size. Never headers, bodies, query strings, or credentials.
        known = any(self.path == "/hooks/" + name for name in self.server.config.routes)
        delivery = next(
            (self.headers.get(name) for name in DELIVERY_HEADERS if self.headers.get(name)), None
        )
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = None
        log_line(
            {
                "event": "webhook_request",
                "method": self.command,
                "path": self.path if known or self.path == "/healthz" else "(other)",
                "status": int(code) if str(code).isdigit() else None,
                "delivery": delivery
                if delivery and re.fullmatch(r"[A-Za-z0-9-]{1,64}", delivery)
                else None,
                "bytes": size,
            }
        )

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
                route, raw, dict(self.headers), self.server.secrets[route.name], self.server.queue
            )
            self.respond(202, result)
        except CirraError as exc:
            code = 503 if "capacity" in str(exc) or "another invocation" in str(exc) else 400
            if "authentication" in str(exc):
                code = 401
            self.respond(
                code, {"error": "delivery refused; check authentication, route, or capacity"}
            )
        except (TimeoutError, ValueError, OSError, RecursionError):
            self.respond(400, {"error": "invalid or interrupted delivery"})
