"""Report native health to independent per-host and active-worker Kuma monitors."""

import argparse
import http.client
import ipaddress
import json
import os
import re
import socket
import stat
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from cirra.audits import repository_key
from cirra.health import DEFAULT_HEARTBEAT_AGE, check
from cirra.service_config import load_service

MAX_URL_BYTES = 2048
MAX_RESPONSE_BYTES = 4096
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128")
)


def systemd_credential(path, info):
    """systemd uses a root-owned 0440 ACL file in a private service mount."""
    directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
    return (
        re.fullmatch(r"/run/credentials/[A-Za-z0-9_.@-]+\.service", directory) is not None
        and path.parent == Path(directory)
        and info.st_uid == info.st_gid == 0
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) == 0o440
    )


def read_push_url(path):
    """Only a private regular credential file may provide a push destination."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("heartbeat credential must be a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        current = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            or (
                os.name == "posix"
                and current.st_mode & 0o077
                and not systemd_credential(path, current)
            )
        ):
            raise ValueError("heartbeat credential file is not private")
        raw = stream.read(MAX_URL_BYTES + 1)
    if not 0 < len(raw) <= MAX_URL_BYTES:
        raise ValueError("heartbeat credential exceeds its size limit")
    value = raw.decode("utf-8").strip()
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("invalid heartbeat destination")
    parts = urlsplit(value)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or not re.fullmatch(r"/api/push/[A-Za-z0-9_-]{10,200}", parts.path)
        or (parts.port is not None and not 1 <= parts.port <= 65535)
    ):
        raise ValueError("invalid heartbeat destination")
    if parts.scheme == "http":
        address = ipaddress.ip_address(parts.hostname)
        if not any(address in network for network in PRIVATE_NETWORKS):
            raise ValueError("plaintext heartbeat requires a private literal IP address")
    return parts


def push(parts, healthy, timeout):
    """No redirects, retries, response echo or unbounded response body."""
    client = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    connection = client(parts.hostname, parts.port, timeout=timeout)
    timer = None
    deadline = time.monotonic() + timeout
    try:
        connection.connect()
        transport = connection.sock

        def expire():
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        timer = threading.Timer(remaining, expire)
        timer.daemon = True
        timer.start()
        query = urlencode({"status": "up" if healthy else "down", "msg": "Cirra health"})
        connection.request("GET", parts.path + "?" + query, headers={"Connection": "close"})
        response = connection.getresponse()
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if response.status != 200 or len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("heartbeat receiver refused the update")
        if time.monotonic() > deadline or json.loads(body).get("ok") is not True:
            raise ValueError("heartbeat receiver did not acknowledge the update")
    finally:
        if timer is not None:
            timer.cancel()
        connection.close()


def evaluate(args):
    policies = None
    config = None
    if args.service_config is not None:
        config = load_service(args.service_config)
        policies = {}
        for route in config.routes.values():
            if route.policy.audit is not None:
                key = repository_key(
                    {
                        name: getattr(route, name)
                        for name in ("provider", "instance", "repository_id")
                    }
                )
                age = route.policy.audit.max_age_days
                policies[key] = min(policies.get(key, age), age)
    return check(
        args.state_dir,
        max_heartbeat_age=args.max_heartbeat_age,
        audit_policies=policies,
        service_config=config,
        include_metrics=True,
    )


def run(args):
    try:
        health = evaluate(args)
        role = health["recovery_metrics"]["worker_state"]
        problems = sorted({item["kind"] for item in health["problems"]})
        warnings = sorted({item["kind"] for item in health["warnings"]})
    except Exception:
        health, role, problems, warnings = {"healthy": False}, None, ["health_unavailable"], []
    result = {
        "healthy": health["healthy"],
        "worker_state": role,
        "problems": problems,
        "warnings": warnings,
        "host_heartbeat": "failed",
        "active_heartbeat": "skipped",
    }
    # A stale running status must not keep the shared monitor alive. A standby
    # sends only its own host heartbeat; it can never overwrite active health.
    active = role == "running" and not {"heartbeat", "coordination"}.intersection(problems)
    destinations = [("host_heartbeat", args.host_heartbeat_file)]
    if active:
        destinations.append(("active_heartbeat", args.active_heartbeat_file))
    for name, path in destinations:
        result[name] = "failed"
        try:
            push(read_push_url(path), health["healthy"], args.timeout)
            result[name] = "sent"
        except Exception:
            pass  # Neither credential URLs nor provider response text enter logs.
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--service-config", type=Path)
    parser.add_argument("--host-heartbeat-file", type=Path, required=True)
    parser.add_argument("--active-heartbeat-file", type=Path, required=True)
    parser.add_argument("--max-heartbeat-age", type=int, default=DEFAULT_HEARTBEAT_AGE)
    parser.add_argument("--timeout", type=float, default=5)
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 30 or not 0 < args.max_heartbeat_age <= 86400:
        parser.error("heartbeat timeout or age limit is out of range")
    result = run(args)
    print(json.dumps(result, sort_keys=True))
    failed = any(result[name] == "failed" for name in ("host_heartbeat", "active_heartbeat"))
    return 0 if result["healthy"] and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
