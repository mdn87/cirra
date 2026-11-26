"""Exercise the production coordinated service with disposable loopback voters and synthetic events.

Run with the project's Python and --etcd PATH. --evidence saves the public test measurements.
No provider credential, repository, model, Docker daemon, or pre-existing state is needed.
"""

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import runpy
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from pathlib import Path
from unittest.mock import patch

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[1]
# The interpreter may belong to a different worktree. Exercise this checkout's runtime.
# ruff: noqa: E402
sys.path.insert(0, str(ROOT / "src"))

from cirra.audit_worker import AuditCheckPublisher
from cirra.cli import main as cirra_main
from cirra.domain import canonical, digest
from cirra.github import GitHubClient
from cirra.private_transfer import capture_quiesced, restore_into
from cirra.publish import write_receipt
from cirra.quorum import OWNER_KEY, OwnershipLost, Quorum, QuorumUnavailable, bind_owner
from cirra.quorum_budget import enrollment_locks
from cirra.quorum_budget import key as budget_key
from cirra.service_config import load_service
from cirra.store import Store
from cirra.transport import RemoteUncertain

helpers = runpy.run_path(str(ROOT / "scripts" / "rehearse-publisher-quorum.py"))
require, until = helpers["require"], helpers["until"]
HOST = "127.0.0.1"
REPO = "fixture/rehearsal"
REPO_ID = 42
CHANGE = 17
APP_ID = 71
HEAD = "b" * 40
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def certificate_files(root):
    now = datetime.now(UTC)

    def issue(name, *, signer=None, authority=None):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(authority.subject if authority else subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=authority is None, path_length=None), True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        )
        if authority:
            builder = (
                builder.add_extension(
                    x509.ExtendedKeyUsage(
                        [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
                    ),
                    False,
                )
                .add_extension(
                    x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(HOST))]), False
                )
                .add_extension(
                    x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), False
                )
            )
        else:
            builder = builder.add_extension(
                x509.KeyUsage(False, False, False, False, False, True, True, False, False), True
            )
        cert = builder.sign(signer or key, hashes.SHA256())
        (root / (name + ".crt")).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        if authority:
            (root / (name + ".key")).write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
        return key, cert

    ca_key, ca = issue("ca")
    for name in ("voter-0", "voter-1", "voter-2", "primary", "standby"):
        issue(name, signer=ca_key, authority=ca)


def ports(count):
    with ExitStack() as stack:
        sockets = [stack.enter_context(closing(socket.socket())) for _ in range(count)]
        for listener in sockets:
            listener.bind((HOST, 0))
        return [listener.getsockname()[1] for listener in sockets]


def environment():
    system = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "TMP", "TEMP", "TMPDIR"}
    return {
        **{k: v for k, v in os.environ.items() if k.upper() in system},
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "REHEARSAL_HOOK": secrets.token_hex(32),
        "REHEARSAL_ROUTING": secrets.token_hex(32),
        "REHEARSAL_WITNESS": secrets.token_hex(32),
    }


def start(command, log, env):
    with log.open("ab") as output:
        return subprocess.Popen(
            [str(part) for part in command],
            env=env,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=HIDDEN,
        )


def stop(process):
    if process and process.poll() is None:
        process.kill()
        process.wait(timeout=10)


class Voters:
    def __init__(self, binary, root, env, numbers):
        self.binary, self.root, self.env = binary, root, env
        self.clients = numbers[:3]
        self.peers = [f"https://{HOST}:{port}" for port in numbers[3:6]]
        self.cluster = ",".join(f"voter-{i}={url}" for i, url in enumerate(self.peers))
        self.token = secrets.token_hex(12)
        self.procs = [None] * 3
        self.context = ssl.create_default_context(cafile=str(root / "ca.crt"))
        self.context.load_cert_chain(root / "standby.crt", root / "standby.key")

    def start(self, i):
        name = f"voter-{i}"
        data = self.root / ("data-" + name)
        url, peer = f"https://{HOST}:{self.clients[i]}", self.peers[i]
        cert, key, ca = (
            self.root / (name + ".crt"),
            self.root / (name + ".key"),
            self.root / "ca.crt",
        )
        self.procs[i] = start(
            [
                self.binary,
                "--name",
                name,
                "--data-dir",
                data,
                "--listen-client-urls",
                url,
                "--advertise-client-urls",
                url,
                "--listen-peer-urls",
                peer,
                "--initial-advertise-peer-urls",
                peer,
                "--initial-cluster",
                self.cluster,
                "--initial-cluster-token",
                self.token,
                "--initial-cluster-state",
                "existing" if data.exists() else "new",
                "--cert-file",
                cert,
                "--key-file",
                key,
                "--trusted-ca-file",
                ca,
                "--client-cert-auth=true",
                "--peer-cert-file",
                cert,
                "--peer-key-file",
                key,
                "--peer-trusted-ca-file",
                ca,
                "--peer-client-cert-auth=true",
                "--enable-grpc-gateway=true",
                "--log-level",
                "warn",
            ],
            self.root / (name + ".log"),
            self.env,
        )

    def api(self, i, path, payload, *, context=None):
        with closing(
            HTTPSConnection(HOST, self.clients[i], timeout=3, context=context or self.context)
        ) as client:
            client.request(
                "POST", "/v3/" + path, canonical(payload), {"Content-Type": "application/json"}
            )
            response = client.getresponse()
            body = json.loads(response.read())
            if response.status != 200 or not body.get("header"):
                raise ConnectionError(f"voter {i} did not confirm {path} (HTTP {response.status})")
            return body

    def ready(self):
        for i, process in enumerate(self.procs):
            if process.poll() is not None:
                log = self.root / f"voter-{i}.log"
                raise RuntimeError(f"voter {i} exited: " + log.read_text(errors="replace")[-1000:])
        statuses = [self.api(i, "maintenance/status", {}) for i in range(3)]
        ids = {s["header"]["cluster_id"] for s in statuses}
        leaders = {s.get("leader") for s in statuses}
        return (
            ids.pop()
            if len(ids) == len(leaders) == 1 and leaders.isdisjoint({None, 0, "0"})
            else None
        )

    def read(self, i, key):
        encoded = base64.b64encode(("/cirra/rehearsal/" + key).encode()).decode()
        cells = self.api(i, "kv/range", {"key": encoded}).get("kvs", [])
        return base64.b64decode(cells[0]["value"]).decode() if cells else None

    def close(self):
        for process in self.procs:
            stop(process)


def service_file(root, node, voters, cluster_id):
    policy = yaml.safe_load(
        (ROOT / "examples/github-worker-policy.yaml").read_text(encoding="utf8")
    )
    policy["repositories"] = [
        {"provider": "github", "instance": "https://github.com", "repository_id": str(REPO_ID)}
    ]
    for check in policy["ci"]["expected"]:
        check["source_id"] = str(APP_ID)
        check["definition_digest"] = digest("rehearsal observer definition")
    (root / "policy.yaml").write_text(yaml.safe_dump(policy), encoding="utf8")
    (root / "observer.yaml").write_bytes((ROOT / "examples/observer.yaml").read_bytes())
    route = {
        "name": "fixture",
        "provider": "github",
        "repository": REPO,
        "repository_id": str(REPO_ID),
        "policy": "policy.yaml",
        "webhook_secret_env": "REHEARSAL_HOOK",
        "witness_key_env": "REHEARSAL_WITNESS",
        "observer": "observer.yaml",
        "publisher": {
            "app_id": APP_ID,
            "installation_id": 11,
            "private_key_env": "REHEARSAL_PUBLISHER",
        },
    }
    order = (0, 1, 2) if node == "primary" else (1, 2, 0)
    policy = {
        "endpoints": [f"https://{HOST}:{voters.clients[i]}" for i in order],
        "namespace": "rehearsal",
        "cluster_id": str(cluster_id),
        "ca": "ca.crt",
        "certificate": node + ".crt",
        "private_key": node + ".key",
        "routing_key_env": "REHEARSAL_ROUTING",
        "node": node,
        "preferred_node": "primary",
        "standby_node": "standby",
        "lease_seconds": 5,
    }
    if os.name == "posix":
        policy["recovery_archive"] = "return.zip" if node == "primary" else "initial.zip"
    config = root / (node + ".yaml")
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "worker": {"poll_seconds": 0.1, "max_jobs_per_day": 1},
                "routes": [route],
                "coordination": policy,
            }
        ),
        encoding="utf8",
    )
    return config


def cli(command, config, state, *args):
    return [
        sys.executable,
        "-B",
        "-m",
        "cirra",
        command,
        "--service-config",
        config,
        "--state-dir",
        state,
        *args,
    ]


def run(command, env, *, refusal=None):
    result = subprocess.run(
        [str(part) for part in command],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=HIDDEN,
    )
    output = (result.stderr + result.stdout)[-1800:]
    if refusal:
        require(result.returncode != 0 and refusal in output, "missing expected refusal: " + output)
    else:
        require(result.returncode == 0, output)


def witness(root, key, name):
    statement = {"version": 2, "fixture": name}
    signed = {
        "statement": statement,
        "hmac_sha256": hmac.new(key, canonical(statement).encode(), hashlib.sha256).hexdigest(),
    }
    path = Path("observer") / (digest(name) + "-execution-witness.json")
    write_receipt(root / path, signed)
    return path


def transfer(source, destination, archive, witness_key, *, install=None):
    with enrollment_locks(source):
        authority = json.loads((source / "data-authority.json").read_text())
        capture_quiesced(source, archive, witness_key)
    if install is None:
        restore_into(archive, destination, witness_key)
    else:
        install()
    with Store(destination, readonly=True).connection() as connection:
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        require(
            integrity == ["ok"] and not foreign_keys, "installed SQLite copy failed verification"
        )
    receipt = json.loads((destination / "data-restored.json").read_text())
    require(
        all(receipt.get(field) == authority[field] for field in ("host", "epoch", "tip")),
        "installed private evidence does not match the captured data-host generation",
    )
    records = sorted((destination / "observer").glob("*-execution-witness.json"))
    historical = 0
    for record in records:
        row = json.loads(record.read_text())
        signature = hmac.new(witness_key, canonical(row["statement"]).encode(), hashlib.sha256)
        require(
            hmac.compare_digest(signature.hexdigest(), row["hmac_sha256"]), "bad restored witness"
        )
        history = record.parent / "history" / record.stem / (digest(row) + ".json")
        require(history.read_bytes() == record.read_bytes(), "restored witness history differs")
        historical += 1
    require(bool(records), "verified copy is missing the synthetic witness")
    return {
        "snapshot_id": receipt["snapshot_id"],
        "archive_bytes": archive.stat().st_size,
        "sqlite_integrity": integrity[0],
        "foreign_key_errors": len(foreign_keys),
        "current_witnesses": len(records),
        "historical_witnesses": historical,
    }


def http(port, path, body=None, headers=None):
    with closing(HTTPConnection(HOST, port, timeout=12)) as client:
        client.request("GET" if body is None else "POST", path, body=body, headers=headers or {})
        response = client.getresponse()
        return response.status, json.loads(response.read())


def payload(change, action="opened"):
    return canonical(
        {
            "action": action,
            "repository": {"id": REPO_ID, "full_name": REPO},
            "pull_request": {"number": change, "author_association": "OWNER"},
        }
    ).encode()


def post(port, raw, identity, secret):
    return http(
        port,
        "/hooks/fixture",
        raw,
        {
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": identity,
            "X-Hub-Signature-256": "sha256="
            + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
        },
    )


def fixture_worker(config, state, trace, age_hours="0"):
    # The event was signed while open. This provider now reports it closed, so the real worker
    # verifies its identity, charges the attempt, and finishes without calling CI or a model.
    calls = []
    change = {
        "number": CHANGE,
        "base": {"repo": {"id": REPO_ID}},
        "state": "closed",
        "draft": False,
    }

    def get(client, route="", params=None):
        require(client.repository == REPO, "provider received an unexpected repository")
        calls.append([route, params])
        if route == "" and not params:
            return {"id": REPO_ID}, False
        if route == "pulls" and params == {"state": "all", "per_page": 100, "page": 1}:
            return [change], False
        if route == f"pulls/{CHANGE}" and not params:
            return change, False
        raise RuntimeError("unexpected synthetic provider request")

    def loopback(event, args):
        if event == "socket.connect":
            require(args[1][0] == HOST, "worker attempted a connection outside the rehearsal")

    sys.addaudithook(loopback)
    with ExitStack() as fixtures:
        fixtures.enter_context(patch.object(GitHubClient, "get", get))
        if int(age_hours):
            at = int(time.time()) + int(age_hours) * 3600
            fixtures.enter_context(patch("cirra.data_authority._stamp", return_value=at))
        status = cirra_main(["worker", "--once", "--service-config", config, "--state-dir", state])
    Path(trace).write_text(json.dumps(calls), encoding="utf8")
    return status


class CheckTransport:
    """Store the first check, lose its response, and delay visibility on subsequent reads."""

    def __init__(self):
        self.writes = []
        self.visible = False

    def request(self, method, path, params=None, *, body=None):
        if method == "GET" and path == f"/repos/{REPO}/commits/{HEAD}/check-runs":
            return {"check_runs": self.writes if self.visible else []}, False
        require(
            method == "POST" and path == f"/repos/{REPO}/check-runs", "unexpected publisher request"
        )
        native = {**body, "id": 100 + len(self.writes), "app": {"id": APP_ID}}
        self.writes.append(native)
        raise RemoteUncertain("fixture lost an acknowledgement after storing the request")


def uncertain(operation):
    try:
        operation()
    except RemoteUncertain as exc:
        return exc
    raise RuntimeError("expected publication to stop with an uncertain result")


def exercise(binary, root):
    env, numbers = environment(), ports(8)
    certificate_files(root)
    voters = Voters(binary, root, env, numbers)
    origins = {"primary": numbers[6], "standby": numbers[7]}
    receivers = {}

    def listen(node):
        log = root / (node + "-receiver.log")
        receivers[node] = start(
            cli("serve-webhooks", configs[node], states[node], "--port", str(origins[node])),
            log,
            env,
        )

        def healthy():
            if receivers[node].poll() is not None:
                raise RuntimeError(node + " receiver exited: " + log.read_text()[-1000:])
            return http(origins[node], "/healthz")[0] == 200

        until(node + " receiver health", healthy)

    def worker(node, *, awaiting_copy=False, age_hours=0):
        trace = root / ("provider-reads-" + node + ".json")
        run(
            [
                sys.executable,
                "-B",
                __file__,
                "_worker",
                configs[node],
                states[node],
                trace,
                age_hours,
            ],
            env,
            refusal="needs a verified current private state" if awaiting_copy else None,
        )
        reads = json.loads(trace.read_text(encoding="utf8"))
        if awaiting_copy:
            heartbeat = json.loads((states[node] / "worker-status.json").read_text())
            require(
                heartbeat["state"] == "waiting_for_private_state", "missing private state status"
            )
            require(reads == [], "stale private state allowed worker execution")
        return reads

    def deliver(node, raw, identity):
        return post(origins[node], raw, identity, env["REHEARSAL_HOOK"])

    def all_origins(raw, identity):
        with ThreadPoolExecutor(max_workers=len(origins)) as pool:
            sends = {node: pool.submit(deliver, node, raw, identity) for node in origins}
            return {node: send.result() for node, send in sends.items()}

    with ExitStack() as cleanup:
        cleanup.callback(voters.close)
        cleanup.callback(lambda: [stop(process) for process in receivers.values()])
        for i in range(3):
            voters.start(i)
        cluster_id = until("three TLS voters with an elected leader", voters.ready)
        etcd_version = voters.api(0, "maintenance/status", {})["version"]
        uncredentialed = ssl.create_default_context(cafile=str(root / "ca.crt"))
        for i in range(3):
            try:
                voters.api(i, "maintenance/status", {}, context=uncredentialed)
            except (ssl.SSLError, HTTPException, ConnectionResetError):
                continue
            raise RuntimeError(f"voter {i} did not reject a client without a certificate")

        configs = {node: service_file(root, node, voters, cluster_id) for node in origins}
        states = {node: root / ("private-" + node) for node in origins}
        primary, standby = states.values()
        managed = os.name == "posix"
        if managed:
            for selector in states.values():
                original = selector.with_name(selector.name + "-initial")
                original.mkdir(mode=0o700)
                selector.symlink_to(original, target_is_directory=True)
        else:
            primary.mkdir(mode=0o700)
        Store(primary)
        witness_key = env["REHEARSAL_WITNESS"].encode()
        witness(primary, witness_key, "primary baseline")
        run(cli("enroll-coordination", configs["primary"], primary, "--mode", "initialize"), env)
        initial_archive = root / "initial.zip"
        original_standby = standby.resolve()

        def install_initial():
            run(
                cli(
                    "install-standby-snapshot",
                    configs["standby"],
                    standby,
                    "--archive",
                    initial_archive,
                ),
                env,
            )
            require(standby.resolve() != original_standby, "standby selector did not switch")

        if managed:
            run(cli("enroll-coordination", configs["standby"], standby, "--mode", "join"), env)
        initial_copy = transfer(
            primary,
            standby,
            initial_archive,
            witness_key,
            install=install_initial if managed else None,
        )
        require(initial_copy["current_witnesses"] == 1, "standby did not receive the baseline")
        run(cli("enroll-coordination", configs["standby"], standby, "--mode", "join"), env)
        worker("standby", awaiting_copy=True, age_hours=25)
        for node in origins:
            listen(node)
        primary_quorum = Quorum(load_service(configs["primary"]).coordination)
        standby_quorum = Quorum(load_service(configs["standby"]).coordination)

        raw, delivery_id = payload(CHANGE), "synthetic-takeover"
        status, ack = deliver("primary", raw, delivery_id)
        identifier = digest(["fixture", hashlib.sha256(raw).hexdigest()])
        require(
            (status, ack.get("job_id"), ack.get("duplicate")) == (202, identifier, False),
            "first signed delivery was not accepted as new",
        )
        pending, job_key, result_key = (
            "inbox/" + prefix + identifier for prefix in ("pending/", "jobs/", "results/")
        )
        persisted = voters.read(1, pending)
        require(
            persisted
            and voters.read(2, job_key) == digest(persisted)
            and voters.read(1, "inbox/count") == "1",
            "202 lacked a committed job and pending record outside the receiving origin",
        )
        binding = json.loads(persisted)[3]
        require(
            voters.read(2, "inbox/deliveries/" + binding) == identifier,
            "delivery was not bound at 202",
        )
        job_path = Path("queue") / (identifier + ".json")
        require(
            not any((state / job_path).exists() for state in states.values()),
            "a local job predated takeover",
        )
        hints = "coordinated-hints"
        require(
            list((primary / hints).glob("*.json")) and not list((standby / hints).glob("*.json")),
            "standby had advance knowledge of the private change identity",
        )

        old = primary_quorum.acquire()
        require(
            old is not None and standby_quorum.acquire() is None,
            "two owners could enter the same generation",
        )
        transport = CheckTransport()
        publisher = AuditCheckPublisher(REPO, APP_ID, "synthetic", http=transport)

        def publish(slot="fixture-audit"):
            return publisher.publish(HEAD, slot, "success", "Synthetic audit completed")

        with bind_owner(old):
            uncertain(publish)
        require(len(transport.writes) == 1, "original provider did not store exactly one effect")
        effect_key = "effects/" + digest(["audit", [str(APP_ID), "fixture-audit"]])
        original_intent = standby_quorum.get(effect_key)
        require(original_intent, "ambiguous acknowledgement did not pin the publication intent")
        old.renew()
        start_time = time.monotonic()
        stop(receivers["primary"])
        stop(voters.procs[0])
        unavailable = root / "detached-primary"
        original_primary = primary.resolve()
        original_primary.rename(unavailable)
        health_samples = {}

        def released():
            status = http(origins["standby"], "/healthz")[0]
            health_samples[status] = health_samples.get(status, 0) + 1
            try:
                return standby_quorum.get(OWNER_KEY) is None
            except QuorumUnavailable:
                return False

        until("former publishing lease to expire", released)
        expiry_seconds = time.monotonic() - start_time
        calls = worker("standby")
        takeover_seconds = time.monotonic() - start_time
        reads = [path for path, _ in calls]
        require(
            "pulls" in reads
            and f"pulls/{CHANGE}" in reads
            and reads.index("pulls") < reads.index(f"pulls/{CHANGE}"),
            "standby did not recover the change from the provider before normal execution",
        )
        expected_reason = "change is closed or a draft"
        local_job = json.loads((standby / job_path).read_text(encoding="utf8"))
        require(
            local_job["hint"]["change"] == CHANGE
            and local_job["hint"]["delivery"].startswith("quorum:"),
            "standby did not materialize the admitted change in its own queue",
        )
        require(
            local_job["attempts"] == 1
            and local_job["result"] == {"state": "skipped", "reason": expected_reason},
            "the production worker did not process the recovered change exactly once",
        )
        require(
            not primary.exists() and not (unavailable / job_path).exists(),
            "original execution state was reused",
        )
        require(
            voters.read(1, result_key) == "skipped"
            and voters.read(2, pending) is None
            and voters.read(1, "inbox/count") == "0",
            "worker did not acknowledge the exact admission",
        )

        owner = standby_quorum.acquire()
        require(owner is not None, "standby could not acquire the publishing lease")
        with owner.maintaining():
            with bind_owner(old):
                stale = uncertain(lambda: publish("new-slot-on-stale-owner"))
            require(
                isinstance(stale.__cause__, OwnershipLost),
                "unrelated failure masked the stale generation",
            )
            hidden = uncertain(publish)
            require(
                "not visible" in str(hidden) and len(transport.writes) == 1,
                "standby attempted to duplicate an ambiguous, temporarily hidden provider result",
            )
            transport.visible = True
            recovered = publish()
            require(
                recovered == {"native_id": "100", "existing": True} and len(transport.writes) == 1,
                "standby did not confirm the existing provider object",
            )
            require(
                owner.get(effect_key) not in (None, original_intent),
                "exact readback did not confirm the intent",
            )

        voters.start(0)
        require(
            until("rejoined original voter", voters.ready) == cluster_id,
            "rejoined voter changed cluster",
        )
        unavailable.rename(original_primary)
        listen("primary")
        duplicates = all_origins(raw, delivery_id)
        require(
            all(
                code == 202 and body.get("duplicate") is True and body.get("job_id") == identifier
                for code, body in duplicates.values()
            ),
            "origins disagreed about the accepted delivery",
        )
        conflicts = all_origins(payload(CHANGE, "reopened"), delivery_id)
        require(
            all(
                code == 400 and "reused" in body.get("error", "")
                for code, body in conflicts.values()
            ),
            "origins disagreed about a signed conflicting reuse of the delivery identity",
        )
        worker("primary", awaiting_copy=True)
        require(worker("standby") == [], "an accepted duplicate was executed again")
        require(
            not (primary / job_path).exists(), "rejoined primary replayed the already settled job"
        )

        # The old primary stays at its existing recovery point until the active standby
        # fences itself and the private evidence produced in its tenure is installed.
        stop(receivers["primary"])
        new_witness = witness(standby, witness_key, "after standby promotion")
        run(
            cli("freeze-coordination", configs["standby"], standby, "--target-node", "primary"), env
        )
        status_while_frozen, acknowledged = deliver("standby", raw, delivery_id)
        require(
            status_while_frozen == 202 and acknowledged.get("duplicate") is True,
            "the remaining receiver stopped admitting while the publishing worker was frozen",
        )

        def install_return():
            require(worker("primary") == [], "automatic install replayed the settled job")
            require(
                primary.resolve() != original_primary,
                "worker did not switch its stale private root",
            )

        if not managed:
            primary.rename(root / "superseded-primary")
        return_copy = transfer(
            standby,
            primary,
            root / "return.zip",
            witness_key,
            install=install_return if managed else None,
        )
        require(return_copy["current_witnesses"] == 2, "return transfer missed active evidence")
        require(
            (primary / new_witness).read_bytes() == (standby / new_witness).read_bytes()
            and (primary / job_path).read_bytes() == (standby / job_path).read_bytes(),
            "installed return copy lacks the private witness or the settled job",
        )
        run(cli("enroll-coordination", configs["primary"], primary, "--mode", "join"), env)
        if not managed:
            require(worker("primary") == [], "freshly returned primary repeated the settled job")
        worker("standby", awaiting_copy=True)
        listen("primary")

        # Leave exactly one voter and both actual receivers alive.
        stop(voters.procs[1])
        stop(voters.procs[2])
        minority_raw = payload(CHANGE + 1)
        refused = all_origins(minority_raw, "synthetic-minority")
        require(
            all(code == 503 for code, _ in refused.values()),
            "an origin acknowledged a minority admission",
        )
        try:
            isolated_owner = primary_quorum.acquire()
        except QuorumUnavailable:
            pass
        else:
            raise RuntimeError(
                f"a minority returned an ownership decision ({bool(isolated_owner)})"
            )
        voters.start(1)
        voters.start(2)
        require(
            until("second quorum rejoin", voters.ready) == cluster_id,
            "cluster identity changed after isolation",
        )
        minority_id = digest(["fixture", hashlib.sha256(minority_raw).hexdigest()])
        day = local_job["started_at"][:10]
        daily_key = budget_key("usage", "review", day)
        final_usage = json.loads(voters.read(0, daily_key))
        require(
            final_usage == {"attempts": 1, "maximum": 1},
            "final shared daily quota did not equal one attempt",
        )
        for i in range(3):
            require(
                voters.read(i, "inbox/jobs/" + minority_id) is None
                and voters.read(i, "inbox/pending/" + minority_id) is None
                and voters.read(i, result_key) == "skipped"
                and voters.read(i, "inbox/count") == "0"
                and json.loads(voters.read(i, daily_key)) == final_usage,
                "rejoined voters disagreed on settled work, refused work, or quota",
            )
        require(
            all(http(port, "/healthz")[0] == 200 for port in origins.values()),
            "origin did not recover readiness",
        )
        retried = all_origins(minority_raw, "synthetic-minority")
        require(
            all(
                code == 202 and body.get("job_id") == minority_id for code, body in retried.values()
            )
            and sorted(body["duplicate"] for _, body in retried.values()) == [False, True],
            "concurrent redelivery after rejoin did not create exactly one new obligation",
        )
        worker("standby", awaiting_copy=True)
        require(worker("primary") == [], "the owner called the provider at its daily quota")
        queued = json.loads((primary / "queue" / (minority_id + ".json")).read_text())
        require(
            queued["state"] == "queued" and queued["attempts"] == 0,
            "the new job ran or was discarded at the shared quota limit",
        )
        for i in range(3):
            require(
                voters.read(i, "inbox/count") == "1"
                and voters.read(i, "inbox/pending/" + minority_id)
                and json.loads(voters.read(i, daily_key)) == final_usage,
                "post-rejoin admission changed the exhausted budget or lost the pending job",
            )
        budgets, more = standby_quorum.scan("budgets/")
        reservations = [json.loads(value) for _, value in budgets if "phase" in json.loads(value)]
        require(
            not more
            and len(reservations) == 1
            and reservations[0]["phase"] == "charged"
            and reservations[0]["ledger"] == daily_key,
            "a second reservation or refund survived takeover",
        )

    return {
        "utc": datetime.now(UTC).isoformat(),
        "etcd_voters": 3,
        "etcd_version": etcd_version,
        "tls_clients_without_certificates_rejected": 3,
        "private_state_roots": 2,
        "state_installation": (
            "managed selector: CLI install to standby, automatic worker install on return"
            if managed
            else "verified restore into empty private directories"
        ),
        "standby_receiver_status_during_frozen_transfer": status_while_frozen,
        "primary_to_standby_verified_copy": initial_copy,
        "standby_to_primary_verified_copy": return_copy,
        "returned_primary_waited_for_private_state": True,
        "former_standby_waited_after_failback": True,
        "verified_copy_refused_at_simulated_age_hours": 25,
        "receiver_command": "cirra serve-webhooks",
        "worker_command": "cirra worker --once",
        "status_before_any_local_job": status,
        "other_voters_confirmed_admission": 2,
        "former_lease_expiry_seconds": round(expiry_seconds, 2),
        "time_to_standby_worker_exit_seconds": round(takeover_seconds, 2),
        "standby_health_samples_during_lease_expiry": health_samples,
        "standby_provider_reads": reads,
        "detached_primary_during_execution": True,
        "worker_result": local_job["result"],
        "worker_attempts": local_job["attempts"],
        "dual_origin_duplicate_status": {k: v[0] for k, v in duplicates.items()},
        "dual_origin_conflict_status": {k: v[0] for k, v in conflicts.items()},
        "dual_origin_minority_status": {k: v[0] for k, v in refused.items()},
        "minority_created_jobs": 0,
        "rejoined_voters_checked": 3,
        "post_rejoin_concurrent_redelivery": {
            k: {"status": code, "duplicate": body["duplicate"]}
            for k, (code, body) in retried.items()
        },
        "final_pending_jobs_at_daily_quota": 1,
        "charged_reservations": len(reservations),
        "shared_review_usage": final_usage,
        "audit_check_provider_posts_after_lost_ack": len(transport.writes),
        "expired_owner_send_refused": True,
        "recovered_native_id": recovered["native_id"],
        "fixture_boundaries": [
            "One machine with three independent etcd processes and two loopback receivers.",
            "Only the GitHub repository, change-list and change reads used a fixture.",
            "The copy-age test advanced the worker's freshness clock by 25 hours.",
            "The synthetic change had closed since admission, so no model or Docker job ran.",
            "The audit-check transport lost a write acknowledgement and delayed read visibility.",
            "Audit-check fencing ran under real leases independently of the closed review job.",
        ],
    }


def main():
    if sys.argv[1:2] == ["_worker"]:
        return fixture_worker(*sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--etcd", type=Path, required=True, help="path to the etcd executable")
    parser.add_argument("--evidence", type=Path, help="write the sanitized result as JSON")
    args = parser.parse_args()
    binary = args.etcd.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="cirra-service-rehearsal-") as directory:
        result = exercise(binary, Path(directory))
    evidence = json.dumps(result, indent=2)
    if args.evidence:
        args.evidence.write_text(evidence + "\n", encoding="utf8")
    print(evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
