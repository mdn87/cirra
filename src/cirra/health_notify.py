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
    try:
        result = subprocess.run(
            ["journalctl", "-u", "cirra-health.service", "-n", "1", "-o", "cat", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()[-900:] if result.returncode == 0 else ""


def receiver(host, port):
    if host == "localhost":
        host = "127.0.0.1"
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    connection = http.client.HTTPConnection if loopback else http.client.HTTPSConnection
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
