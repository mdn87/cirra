"""Exercise publisher takeover using disposable loopback etcd members and a synthetic provider."""

import argparse
import base64
import hashlib
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from functools import partial
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOOPBACK = "127.0.0.1"
OWNER = "/owner"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def until(description, check, timeout=25):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            if result := check():
                return result
        except (ConnectionError, TimeoutError) as exc:
            last = exc
        time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {description}") from last


def request(port, method, path, data=None, timeout=2):
    with closing(HTTPConnection(LOOPBACK, port, timeout=timeout)) as client:
        body = json.dumps(data).encode() if data is not None else None
        client.request(method, path, body=body, headers={"Content-Type": "application/json"})
        response = client.getresponse()
        return response.status, json.loads(response.read())


def encode(value):
    return base64.b64encode(value.encode()).decode()


class Member:
    def __init__(self, name, client_port, peer_port, root):
        self.name, self.port = name, client_port
        self.peer = f"http://{LOOPBACK}:{peer_port}"
        self.data = root / name
        self.process = None

    def start(self, binary, peers, cluster_token):
        url = f"http://{LOOPBACK}:{self.port}"
        # The subprocess needs neither provider credentials nor an operator's ETCD_* settings.
        system_keys = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "TMP", "TEMP", "TMPDIR"}
        env = {k: v for k, v in os.environ.items() if k.upper() in system_keys}
        with self.data.with_suffix(".log").open("ab") as log:
            self.process = subprocess.Popen(
                [
                    str(binary),
                    "--name",
                    self.name,
                    "--data-dir",
                    str(self.data),
                    "--listen-client-urls",
                    url,
                    "--advertise-client-urls",
                    url,
                    "--listen-peer-urls",
                    self.peer,
                    "--initial-advertise-peer-urls",
                    self.peer,
                    "--initial-cluster",
                    peers,
                    "--initial-cluster-token",
                    cluster_token,
                    "--initial-cluster-state",
                    "existing" if self.data.exists() else "new",
                    "--enable-grpc-gateway=true",
                    "--log-level",
                    "warn",
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)

    def api(self, route, data, timeout=2):
        code, result = request(self.port, "POST", "/v3/" + route, data, timeout)
        if code in (408, 503, 504) or result.get("code") in (4, 14):
            raise ConnectionError(f"etcd unavailable: {code}: {result.get('message', '')}")
        require(code == 200 and "header" in result, f"etcd {route} failed: {code}: {result}")
        return result

    def status(self):
        return self.api("maintenance/status", {})

    def get(self, key, *, local=False):
        result = self.api("kv/range", {"key": encode(key), "serializable": local})
        values = result.get("kvs")
        return base64.b64decode(values[0]["value"]).decode() if values else None

    def txn(self, checks, key, value, *, lease=None, timeout=2):
        compare = []
        for name, expected in checks:
            condition = {"key": encode(name), "result": "EQUAL"}
            if expected is None:
                condition.update(target="VERSION", version="0")
            else:
                condition.update(target="VALUE", value=encode(expected))
            compare.append(condition)
        put = {"key": encode(key), "value": encode(value)}
        if lease is not None:
            put["lease"] = lease
        result = self.api(
            "kv/txn", {"compare": compare, "success": [{"request_put": put}]}, timeout
        )
        return result.get("succeeded", False)

    def grant(self, owner, seconds):
        lease = self.api("lease/grant", {"TTL": str(seconds)})["ID"]
        return self.txn([(OWNER, None)], OWNER, owner, lease=lease)

    def reserve(self, owner, slot, intent, previous=None):
        expected = "observed/" + previous if previous else None
        return self.txn([(OWNER, owner), (slot, expected)], slot, "pending/" + intent)

    def confirm(self, owner, slot, intent):
        return self.txn([(OWNER, owner), (slot, "pending/" + intent)], slot, "observed/" + intent)


def fingerprint(document):
    content = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(content).hexdigest()


def reserve_post(member, provider, owner, document, previous=None):
    slot = document["slot"]
    if member.reserve(owner, slot, fingerprint(document), previous):
        return partial(request, provider.server_port, "POST", slot, document, 35)
    return None


def competing_post(member, provider, owner, document, previous=None):
    send = reserve_post(member, provider, owner, document, previous)
    if send:
        send()
        raise RuntimeError("a competing sender wrote to the provider")
    provider.refusals += 1


class ProviderHandler(BaseHTTPRequestHandler):
    def reply(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not self.path.startswith("/slots/") or data.get("slot") != self.path:
            return self.reply(400, {})
        self.server.writes.append(data)
        self.server.received.set()
        self.server.allow_reply.wait(30)
        self.reply(201, data)

    def do_GET(self):
        visible = self.server.visible
        if visible is None or visible["slot"] != self.path:
            return self.reply(404, {})
        self.reply(200, visible)

    def log_message(self, *args):
        pass


def recover(member, provider, owner, slot, intent):
    status, document = request(provider.server_port, "GET", slot)
    if status == 404:
        return False
    require(status == 200, "provider readback failed")
    return (
        document.get("slot") == slot
        and fingerprint(document) == intent
        and member.confirm(owner, slot, intent)
    )


def healthy(members):
    statuses = [member.status() for member in members]
    ids = {s["header"]["member_id"] for s in statuses}
    leaders = {s.get("leader") for s in statuses}
    if len(ids) == len(members) and len(leaders) == 1 and next(iter(leaders)) in ids:
        return statuses
    return None


def exercise(members, start, provider):
    initial = until("all three voters to agree on a leader", lambda: healthy(members))
    cluster = initial[0]["header"]["cluster_id"]
    first = next(
        m for m, s in zip(members, initial, strict=True) if s["header"]["member_id"] == s["leader"]
    )
    others = [m for m in members if m is not first]
    primary, standby = uuid.uuid4().hex, uuid.uuid4().hex
    slot, other_slot = ("/slots/" + uuid.uuid4().hex for _ in range(2))
    document = {"slot": slot, "generation": uuid.uuid4().hex, "summary": "synthetic review"}
    next_document = {**document, "generation": uuid.uuid4().hex}
    original, following = fingerprint(document), fingerprint(next_document)
    require(first.grant(primary, 4), "initial owner could not acquire the lease")
    require(not first.grant(standby, 60), "a live owner was displaced")
    send_original = reserve_post(first, provider, primary, document)
    require(send_original is not None, "initial operation was not reserved")
    # Pause the old sender here. Its intent must outlive its owner lease.
    first.stop()
    statuses = until("the two survivors to elect their leader", lambda: healthy(others))
    leader = next(
        m for m, s in zip(others, statuses, strict=True) if s["header"]["member_id"] == s["leader"]
    )
    survivor = next(m for m in others if m is not leader)
    marker = "/admissions/" + uuid.uuid4().hex
    opaque = uuid.uuid4().hex
    require(leader.txn([(marker, None)], marker, opaque), "two voters could not admit metadata")
    until("the original etcd lease to expire", lambda: leader.get(OWNER) is None)
    require(leader.grant(standby, 90), "survivor could not acquire the expired lease")
    competing_post(leader, provider, primary, {**document, "slot": other_slot})
    require(leader.reserve(standby, other_slot, opaque), "new owner could not reserve a free slot")
    competing_post(leader, provider, standby, document)
    competing_post(leader, provider, standby, next_document)
    require(not recover(leader, provider, standby, slot, original), "empty read released the slot")

    # The old sender resumes *after* takeover and sends its one previously reserved write.
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(send_original)
        try:
            require(provider.received.wait(5), "synthetic provider never received the write")
            provider.visible = {**document, "summary": "stale review"}
            require(not recover(leader, provider, standby, slot, original), "stale content matched")
            competing_post(leader, provider, standby, next_document, original)
            provider.visible = provider.writes[0]
            require(
                not recover(leader, provider, primary, slot, original), "expired owner confirmed"
            )
            require(
                recover(leader, provider, standby, slot, original), "exact readback did not settle"
            )
            require(
                not leader.reserve(standby, slot, following, opaque), "wrong predecessor matched"
            )
            require(
                leader.reserve(standby, slot, following, original), "next generation was refused"
            )
            require(not future.done(), "old sender received its reply before readback")
        finally:
            provider.allow_reply.set()
        require(future.result() == (201, document), "old sender did not receive the delayed reply")
    require(
        not leader.confirm(primary, slot, original), "old sender acknowledged over its successor"
    )
    require(not leader.confirm(standby, slot, original), "old operation settled a newer intent")
    require(provider.writes == [document], "the synthetic provider received duplicate writes")

    leader.stop()
    until(
        "the lone voter's leader to disappear", lambda: survivor.status().get("leader", "0") == "0"
    )
    minority = "/admissions/" + uuid.uuid4().hex
    try:
        acknowledged = survivor.txn([(minority, None)], minority, opaque, timeout=9)
    except (ConnectionError, TimeoutError):
        acknowledged = False
    require(not acknowledged, "one voter acknowledged a new operation")
    require(survivor.get(minority, local=True) is None, "one voter applied a minority write")
    start(first)
    start(leader)
    final = until("the original members to rejoin", lambda: healthy(members))
    require(
        [s["header"]["member_id"] for s in initial] == [s["header"]["member_id"] for s in final]
        and all(s["header"]["cluster_id"] == cluster for s in final),
        "rejoin replaced the original cluster or one of its members",
    )
    expected = {marker: opaque, slot: "pending/" + following, other_slot: "pending/" + opaque}
    until(
        "all three local stores to reflect committed records",
        lambda: all(m.get(k, local=True) == v for m in members for k, v in expected.items()),
    )
    return {
        "verified_at": datetime.now(UTC).isoformat(),
        "scope": "Loopback processes and a synthetic HTTP provider on one test computer.",
        "etcd_version": final[0]["version"],
        "voters": len(members),
        "admitted_with_two_voters": True,
        "minority_write_acknowledged": False,
        "minority_write_present_after_quorum_returned": members[0].get(minority) is not None,
        "expired_owner_refused": True,
        "pending_slot_survived_owner_loss": True,
        "empty_and_stale_readback_refused": True,
        "exact_readback_allowed_next_generation": True,
        "late_acknowledgement_refused": True,
        "provider_post_requests": len(provider.writes),
        "competing_post_attempts_refused": provider.refusals,
        "rejoined_voters_with_committed_records": len(members),
        "production_worker_and_provider_exercised": False,
    }


def rehearse(binary, root):
    with (
        tempfile.TemporaryDirectory(prefix="cirra-quorum-", dir=root) as temporary,
        ExitStack() as stack,
    ):
        provider = stack.enter_context(ThreadingHTTPServer((LOOPBACK, 0), ProviderHandler))
        # Have the OS select six distinct unused loopback ports without guessing a port range.
        with ExitStack() as reservations:
            ports = []
            for _ in range(6):
                sock = reservations.enter_context(socket.socket())
                sock.bind((LOOPBACK, 0))
                ports.append(sock.getsockname()[1])
        members = [
            Member(f"voter-{i}", ports[2 * i], ports[2 * i + 1], Path(temporary)) for i in range(3)
        ]
        peers = ",".join(f"{member.name}={member.peer}" for member in members)
        token = uuid.uuid4().hex

        def start(member):
            member.start(binary, peers, token)

        for member in members:
            stack.callback(member.stop)
            start(member)
        provider.received, provider.allow_reply = threading.Event(), threading.Event()
        provider.visible = None
        provider.writes = []
        provider.refusals = 0
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        stack.callback(thread.join)
        stack.callback(provider.shutdown)
        return exercise(members, start, provider)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--etcd", type=Path, required=True, help="verified local etcd v3 executable"
    )
    parser.add_argument("--receipt", type=Path, help="also write the JSON results to this file")
    args = parser.parse_args()
    scratch = Path(__file__).resolve().parents[1] / ".lugos-audit"
    scratch.mkdir(exist_ok=True)
    results = rehearse(args.etcd.resolve(strict=True), scratch)
    results["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    output = json.dumps(results, indent=2) + "\n"
    if args.receipt:
        args.receipt.write_text(output, encoding="utf-8")
    print(output, end="")
