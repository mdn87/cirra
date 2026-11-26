import http.client
import json
import shlex
import subprocess
import threading
from configparser import ConfigParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from cirra import health_notify


@pytest.fixture
def unit(tmp_path):
    marker = tmp_path / "health-alert.pending"

    def run(mode):
        path = Path(__file__).resolve().parents[1] / f"examples/systemd/cirra-health-{mode}.service"
        config = ConfigParser(interpolation=None)
        config.read(path, encoding="utf-8")
        assert config["Service"]["LoadCredentialEncrypted"].startswith("glas-bws-token:")
        exe, launcher, runtime = shlex.split(config["Service"]["ExecStart"])
        assert exe.endswith("/python3") and launcher.endswith("/host-runtime.py")
        name = "cirra-health.json" if mode == "alert" else "cirra-health-resolve.json"
        assert runtime.endswith("/" + name)
        return health_notify.main([mode, str(marker)])

    return run, marker


@pytest.fixture
def receiver(monkeypatch):
    events = []
    statuses = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            events.append(
                (
                    self.path,
                    self.headers["Authorization"],
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
            )
            self.send_response(statuses.pop(0))
            self.end_headers()

        def log_message(self, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        monkeypatch.setenv("LUGOS_EVENT_HOST", "127.0.0.1")
        monkeypatch.setenv("LUGOS_EVENT_PORT", str(server.server_port))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield statuses, events
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize("value", [None, ""])
def test_missing_credential_fails_without_marking_or_sending(unit, receiver, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("LUGOS_EVENT_AUTH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("LUGOS_EVENT_AUTH_TOKEN", value)
    run, marker = unit
    assert run("alert") == 1
    assert not marker.exists() and not receiver[1]


def test_alert_and_recovery_delivery_cycle(unit, receiver, monkeypatch):
    run, marker = unit
    statuses, events = receiver
    monkeypatch.setenv("LUGOS_EVENT_AUTH_TOKEN", "test-credential")
    statuses.extend([503, 202, 503, 202])

    def stuck_journal(args, *, timeout, **kwargs):
        assert timeout <= 3
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(health_notify.subprocess, "run", stuck_journal)
    assert run("resolve") == 0 and not events
    assert run("alert") == 1 and marker.exists()
    assert run("alert") == 0
    assert marker.read_text().strip() == events[1][2]["id"]
    assert events[1][2]["body"] == "Cirra health check failed on lugos-host"
    monkeypatch.delenv("LUGOS_EVENT_AUTH_TOKEN")
    assert run("resolve") == 1 and marker.exists()
    monkeypatch.setenv("LUGOS_EVENT_AUTH_TOKEN", "test-credential")
    assert run("resolve") == 1 and marker.exists()
    assert run("resolve") == 0 and not marker.exists()
    assert run("resolve") == 0 and not statuses
    assert [e["type"] for _, _, e in events] == [
        "operator_attention.requested",
        "operator_attention.requested",
        "operator_attention.resolved",
        "operator_attention.resolved",
    ]
    for path, auth, event in events:
        assert path == "/events" and auth == "Bearer test-credential"
        assert event["dedupe_key"] == "cirra-health-lugos-host"


def test_event_body_uses_the_service_diagnostic_without_systemd_manager_messages(monkeypatch):
    diagnostic = '{"healthy": false, "problems": [{"kind": "backup", "detail": "backup is stale"}]}'
    lines = iter([diagnostic, '{"healthy": true}', "unstructured service error"])
    monkeypatch.setenv("MONITOR_INVOCATION_ID", "1" * 32)

    def journal(argv, **kwargs):
        assert "_SYSTEMD_UNIT=cirra-health.service" in argv and "_TRANSPORT=stdout" in argv
        assert "_SYSTEMD_INVOCATION_ID=" + "1" * 32 in argv
        return subprocess.CompletedProcess(argv, 0, next(lines) + "\n")

    monkeypatch.setattr(health_notify.subprocess, "run", journal)
    assert health_notify.journal_detail() == diagnostic
    assert health_notify.journal_detail() == ""
    assert health_notify.journal_detail() == ""


def test_local_http_requires_an_explicit_transport(monkeypatch):
    monkeypatch.delenv("LUGOS_EVENT_SCHEME", raising=False)
    for host in ("localhost", "127.0.0.1", "::1"):
        assert type(health_notify.receiver(host, 8787)) is http.client.HTTPConnection
    assert type(health_notify.receiver("10.0.1.33", 8787)) is http.client.HTTPSConnection
    assert type(health_notify.receiver("events.example", 443)) is http.client.HTTPSConnection

    monkeypatch.setenv("LUGOS_EVENT_SCHEME", "http")
    connection = health_notify.receiver("10.0.1.33", 8787)
    endpoints = {"local": "10.0.1.33"}

    class Peer:
        def getsockname(self):
            return endpoints["local"], 54321

        def getpeername(self):
            return "10.0.1.33", 8787

        def close(self):
            pass

    monkeypatch.setattr(
        http.client.HTTPConnection, "connect", lambda conn: setattr(conn, "sock", Peer())
    )
    connection.connect()
    connection.close()
    endpoints["local"] = "10.0.1.22"
    with pytest.raises(ValueError, match="same host"):
        connection.connect()
    assert connection.sock is None
    with pytest.raises(ValueError, match="private IP address"):
        health_notify.receiver("events.example", 8787)

    monkeypatch.setenv("LUGOS_EVENT_SCHEME", "invalid")
    with pytest.raises(ValueError, match="LUGOS_EVENT_SCHEME"):
        health_notify.receiver("10.0.1.33", 8787)
