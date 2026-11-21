"""Deliver health transitions to the operator's configured event receiver."""

import http.client
import ipaddress
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


def journal_detail():
    filters = ["_SYSTEMD_UNIT=cirra-health.service", "_TRANSPORT=stdout"]
    if invocation := os.environ.get("MONITOR_INVOCATION_ID"):
        filters.append("_SYSTEMD_INVOCATION_ID=" + invocation)
    try:
        result = subprocess.run(
            ["journalctl", *filters, "-n", "1", "-o", "cat", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    diagnostic = result.stdout.strip()
    try:
        health = json.loads(diagnostic)
    except (ValueError, RecursionError):
        return ""
    if result.returncode or not isinstance(health, dict) or health.get("healthy") is not False:
        return ""
    return diagnostic[-900:]


def receiver(host, port):
    if host == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    scheme = os.environ.get("LUGOS_EVENT_SCHEME", "auto")
    if scheme not in ("auto", "http", "https"):
        raise ValueError("LUGOS_EVENT_SCHEME must be auto, http, or https")
    if scheme == "http" and (address is None or not address.is_private):
        raise ValueError("LUGOS_EVENT_SCHEME=http requires a private IP address")
    use_http = scheme == "http" or (
        scheme == "auto" and address is not None and address.is_loopback
    )
    connection = http.client.HTTPConnection if use_http else http.client.HTTPSConnection
    return connection(host, port, timeout=8)


def notify(mode, marker):
    if mode == "resolve" and not marker.exists():
        return
    token = os.environ.get("LUGOS_EVENT_AUTH_TOKEN")
    if not token:
        raise ValueError("missing LUGOS_EVENT_AUTH_TOKEN")
    conn = receiver(
        os.environ.get("LUGOS_EVENT_HOST", "127.0.0.1"),
        int(os.environ.get("LUGOS_EVENT_PORT", "8787")),
    )
    event = {
        "schema": "lugos-operator-attention/v1",
        "source": "cirra-health",
        "dedupe_key": "cirra-health-lugos-host",
    }
    if mode == "alert":
        request_id = "attn-" + uuid.uuid4().hex
        # A lost acknowledgement can still mean the receiver accepted the incident.
        marker.write_text(request_id + "\n", encoding="utf-8")
        event.update(
            type="operator_attention.requested",
            id=request_id,
            host="lugos-host",
            title="Cirra needs attention",
            urgency="high",
            ttl_seconds=86400,
            body=journal_detail() or "Cirra health check failed on lugos-host",
        )
    else:
        event.update(type="operator_attention.resolved", reason="run_succeeded")
    try:
        conn.request(
            "POST",
            "/events",
            json.dumps(event).encode(),
            {"Content-Type": "application/json", "Authorization": "Bearer " + token},
        )
        status = conn.getresponse().status
    finally:
        conn.close()
    if not 200 <= status < 300:
        raise ValueError(f"event receiver returned HTTP {status}")
    if mode == "resolve":
        marker.unlink(missing_ok=True)
    print(f"{mode} posted: HTTP {status}")


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in ("alert", "resolve"):
        raise SystemExit("usage: python -m cirra.health_notify {alert,resolve} MARKER")
    try:
        notify(args[0], Path(args[1]))
    except (ValueError, OSError, http.client.HTTPException) as exc:
        print(f"health {args[0]} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
