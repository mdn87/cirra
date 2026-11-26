"""Open-PR failover acceptance. Set CIRRA_TEST_ETCD to a local etcd binary to run it."""

import json
import os
import runpy
import sys
import tempfile
import threading
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import pytest
import yaml

from cirra.cli import main
from cirra.domain import canonical
from cirra.github import NoRedirect
from cirra.publish import NAME, TITLE, metadata_from
from cirra.quorum import OWNER_KEY, Quorum, QuorumUnavailable
from cirra.report import plain
from cirra.service_config import load_service
from cirra.store import Store

if __name__ != "__main__" and not os.environ.get("CIRRA_TEST_ETCD"):
    pytest.skip("set CIRRA_TEST_ETCD to exercise three real TLS voters", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[1]
lab = SimpleNamespace(**runpy.run_path(str(ROOT / "scripts/rehearse-coordinated-service.py")))
REPO = "/repos/" + lab.REPO
HEAD, BASE, TARGET = ("2" * 40, "1" * 40, "3" * 40)
RUNS = f"{REPO}/commits/{HEAD}/check-runs"
TIMESTAMP = "2026-01-02T00:00:00Z"
INSTALLATION = "/app/installations/11"
PERMISSIONS = {"metadata": "read", "checks": "write"}
NOTE = "Synthetic reviewer verified the fixture change."


class FixtureProvider(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def answer(self, code, data):
        raw = canonical(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def checked_app(self):
        import jwt

        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        jwt.decode(token, self.server.public_key, algorithms=["RS256"], issuer=str(lab.APP_ID))

    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        self.server.reads.append(url.path)
        if url.path == INSTALLATION:
            self.checked_app()
            result = {"app_id": lab.APP_ID}
        elif url.path == REPO:
            result = {"id": lab.REPO_ID, "full_name": lab.REPO}
        elif url.path == REPO + "/pulls":
            assert query["state"] == ["all"]
            result = [self.server.pull]
        elif url.path == REPO + f"/pulls/{lab.CHANGE}":
            result = self.server.pull
        elif url.path == REPO + "/branches/main":
            result = {"name": "main", "commit": {"sha": TARGET}}
        elif url.path == REPO + f"/compare/{TARGET}...{HEAD}":
            result = {
                "base_commit": {"sha": TARGET},
                "merge_base_commit": {"sha": BASE},
                "files": [
                    {
                        "filename": "answer.py",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 1,
                        "patch": "@@ -1 +1 @@\n-answer = 0\n+answer = 42\n",
                    }
                ],
            }
        elif url.path == REPO + f"/commits/{HEAD}/statuses":
            result = []
        elif url.path == RUNS:
            checks = [self.server.build, *self.server.native.values()]
            if name := query.get("check_name"):
                checks = [check for check in checks if check["name"] in name]
            result = {"check_runs": checks, "total_count": len(checks)}
        else:
            return self.answer(404, {"error": "unrecognized synthetic read", "route": url.path})
        self.answer(200, result)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/fixture/review":
            self.server.model_packets.append(body)
            return self.answer(200, {"findings": [], "notes": [NOTE]})
        if self.path == INSTALLATION + "/access_tokens":
            self.checked_app()
            assert body == {"repository_ids": [lab.REPO_ID], "permissions": PERMISSIONS}
            return self.answer(
                201,
                {
                    "token": "fixture-token",
                    "permissions": PERMISSIONS,
                    "repositories": [{"id": lab.REPO_ID}],
                },
            )
        if self.path != REPO + "/check-runs":
            return self.answer(404, {"error": "unrecognized synthetic write"})
        assert self.headers["Authorization"] == "Bearer fixture-token"
        native_id = 1000 + len(self.server.native)
        item = {
            **body,
            "id": native_id,
            "app": {"id": lab.APP_ID},
            "check_suite": {"id": 2000},
            "started_at": TIMESTAMP,
            "html_url": "https://example.invalid/check/" + str(native_id),
        }
        self.server.native[native_id] = item
        self.server.writes.append(("POST", native_id))
        self.answer(201, item)

    def do_PATCH(self):
        # Expose any second write, including an unnecessary update to the original ID.
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        native_id = int(self.path.removeprefix(REPO + "/check-runs/"))
        assert self.headers["Authorization"] == "Bearer fixture-token"
        self.server.native[native_id].update(body)
        self.server.writes.append(("PATCH", native_id))
        self.answer(200, self.server.native[native_id])


def fixture_worker(config, state, port, mode):
    """Keep Processor, both GitHub clients, capture, review coordination and publication intact."""
    port = int(port)
    local = build_opener(ProxyHandler({}), NoRedirect())

    class ProviderOpener:
        def open(self, request, *, timeout):
            url = urlsplit(request.full_url)
            assert (url.scheme, url.netloc) == ("https", "api.github.com")
            path = url.path + ("?" + url.query if url.query else "")
            destination = Request(
                f"http://127.0.0.1:{port}" + path,
                data=request.data,
                headers=dict(request.header_items()),
                method=request.get_method(),
            )
            return local.open(destination, timeout=timeout)

    class Reviewer:
        def __init__(self, _config, _scratch):
            pass

        def review(self, packet):
            code, result = lab.http(
                port,
                "/fixture/review",
                canonical(packet).encode(),
                {"Content-Type": "application/json"},
            )
            assert code == 200
            return canonical(result)

    def loopback(event, args):
        if event == "socket.connect" and args[1][0] != lab.HOST:
            raise RuntimeError("fixture worker may connect only to loopback")

    sys.addaudithook(loopback)
    with (
        patch("cirra.github.build_opener", return_value=ProviderOpener()),
        patch("cirra.transport.build_opener", return_value=ProviderOpener()),
        patch("cirra.dispatch.ClaudeReviewer", Reviewer),
    ):
        args = ["worker", "--service-config", config, "--state-dir", state]
        return main(args + (["--once"] if mode == "once" else []))


def configure(root, node, voters, cluster_id):
    path = lab.service_file(root, node, voters, cluster_id)
    service = yaml.safe_load(path.read_text())
    # Upstream CI is pre-existing. This route publishes one native advisory review.
    route = service["routes"][0]
    route.pop("observer")
    route.pop("witness_key_env")
    service["coordination"]["transfer_witness_env"] = "REHEARSAL_WITNESS"
    route["policy"] = "open-review-policy.yaml"
    policy = yaml.safe_load((root / "policy.yaml").read_text())
    policy["ci"]["expected"] = [{"kind": "check_run", "source_id": "93", "name": "fixture-build"}]
    (root / route["policy"]).write_text(yaml.safe_dump(policy))
    service["worker"].update(max_jobs_per_day=20, poll_seconds=60)
    service["coordination"].pop("recovery_archive", None)
    path.write_text(yaml.safe_dump(service))
    return path


def accept_open_change(binary, root):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    env, numbers = lab.environment(), lab.ports(8)
    lab.certificate_files(root)
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    env["REHEARSAL_PUBLISHER"] = signing_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    voters = lab.Voters(binary, root, env, numbers)
    copies = {node: root / ("private-" + node) for node in ("primary", "standby")}
    primary, standby = copies.values()

    with ExitStack() as stack:
        stack.callback(voters.close)
        for i in range(3):
            voters.start(i)
        cluster = lab.until("open-review voters", voters.ready)
        configs = {node: configure(root, node, voters, cluster) for node in copies}
        primary.mkdir()
        Store(primary)
        key = env["REHEARSAL_WITNESS"].encode()
        lab.witness(primary, key, "open-review baseline")
        lab.run(
            lab.cli("enroll-coordination", configs["primary"], primary, "--mode", "initialize"), env
        )
        lab.transfer(primary, standby, root / "baseline.zip", key)
        lab.run(lab.cli("enroll-coordination", configs["standby"], standby, "--mode", "join"), env)
        quorum = Quorum(load_service(configs["standby"]).coordination)

        provider = stack.enter_context(ThreadingHTTPServer((lab.HOST, 0), FixtureProvider))
        provider.public_key = signing_key.public_key()
        provider.native, provider.writes, provider.model_packets, provider.reads = {}, [], [], []
        provider.pull = {
            "number": lab.CHANGE,
            "head": {"sha": HEAD},
            "state": "open",
            "draft": False,
            "author_association": "OWNER",
            "changed_files": 1,
            "base": {"sha": TARGET, "ref": "main", "repo": {"id": lab.REPO_ID}},
        }
        provider.build = {
            "id": 12,
            "name": "fixture-build",
            "app": {"id": 93},
            "check_suite": {"id": 95},
            "head_sha": HEAD,
            "status": "completed",
            "conclusion": "success",
            "started_at": TIMESTAMP,
            "html_url": "https://example.invalid/check/12",
        }
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        stack.callback(thread.join, 5)
        stack.callback(provider.shutdown)

        def worker(node, mode="once"):
            return [
                sys.executable,
                "-B",
                __file__,
                "_worker",
                configs[node],
                copies[node],
                provider.server_port,
                mode,
            ]

        def launch(command, name):
            process = lab.start(command, root / (name + ".log"), env)
            stack.callback(lab.stop, process)
            return process

        primary_worker = launch(worker("primary", "continuous"), "primary-worker")
        lab.until(
            "primary worker to hold the real lease and poll its empty queue",
            lambda: (primary / "worker-status.json").is_file() and quorum.get(OWNER_KEY),
        )
        receivers = {}
        for node, port in zip(copies, numbers[6:], strict=True):
            receivers[node] = launch(
                lab.cli("serve-webhooks", configs[node], copies[node], "--port", port),
                node + "-receiver",
            )
            lab.until(
                node + " HTTP readiness", lambda port=port: lab.http(port, "/healthz")[0] == 200
            )

        body, secret, delivery = (
            lab.payload(lab.CHANGE),
            env["REHEARSAL_HOOK"],
            "initial-open-review",
        )
        status, first = lab.post(numbers[6], body, delivery, secret)
        assert status == 202 and first["duplicate"] is False
        lab.stop(primary_worker)
        lab.stop(receivers["primary"])
        lab.stop(voters.procs[0])
        assert not (primary / "queue" / (first["job_id"] + ".json")).exists()
        assert not list((standby / "coordinated-hints").glob("*.json"))
        primary.rename(root / "offline-primary")
        assert not provider.native and not provider.model_packets

        def release():
            try:
                return quorum.get(OWNER_KEY) is None
            except QuorumUnavailable:
                return False

        lab.until("the killed primary worker lease to expire", release)
        lab.run(worker("standby"), env)

        def result(admission):
            job = json.loads((standby / "queue" / (admission["job_id"] + ".json")).read_text())
            assert job["state"] == "done", job
            assert job["result"]["publication"] == "published", job["result"]
            return job["result"]

        original = result(first)
        store = Store(standby, readonly=True)

        def visible_report():
            metadata = metadata_from(provider.native[1000]["output"]["summary"])
            return store.report_by_digest(metadata["run_id"], metadata["report_digest"])

        initial_report = visible_report()
        assert initial_report["ci"]["expected"][0]["evidence"]["raw_state"] == "success"
        assert REPO + "/pulls" in provider.reads
        assert f"{REPO}/compare/{TARGET}...{HEAD}" in provider.reads
        assert len(provider.model_packets) == 1
        assert "answer.py" in canonical(provider.model_packets[0])
        assert provider.writes == [("POST", 1000)]
        status, duplicate = lab.post(numbers[7], body, delivery, secret)
        assert status == 202 and duplicate == {**first, "duplicate": True}
        lab.run(worker("standby"), env)
        # A distinct authenticated event reaches Processor a second time for this same stable head.
        status, refresh = lab.post(
            numbers[7], lab.payload(lab.CHANGE, "synchronize"), "refresh-open-review", secret
        )
        assert status == 202 and not refresh["duplicate"] and refresh["job_id"] != first["job_id"]
        lab.run(worker("standby"), env)
        replayed = result(refresh)
        assert replayed["native_id"] == original["native_id"] == "1000"
        assert replayed["run_id"] == original["run_id"] and replayed["reused"]
        assert len(provider.model_packets) == 1
        assert provider.writes == [("POST", 1000)], {
            "baseline": [
                (s["name"], s["observed_at"], s["excluded"])
                for s in initial_report["ci"]["evidence"]
            ],
            "refreshed": [
                (s["name"], s["observed_at"], s["excluded"])
                for s in visible_report()["ci"]["evidence"]
            ],
        }
        assert visible_report() == initial_report
        initial_generation = metadata_from(provider.native[1000]["output"]["summary"])["generation"]
        # A real upstream state change must still update the existing native object.
        provider.build["conclusion"] = "failure"
        status, changed = lab.post(
            numbers[7], lab.payload(lab.CHANGE, "reopened"), "changed-ci-review", secret
        )
        assert status == 202 and not changed["duplicate"]
        lab.run(worker("standby"), env)
        updated = result(changed)
        assert (
            updated["native_id"] == original["native_id"]
            and updated["run_id"] == original["run_id"]
        )
        assert updated["reused"] and len(provider.model_packets) == 1
        assert provider.writes == [("POST", 1000), ("PATCH", 1000)]
        assert initial_report["ci"]["expected"][0]["evidence"]["raw_state"] == "success"
        assert visible_report()["ci"]["expected"][0]["evidence"]["raw_state"] == "failure"
        code, visible = lab.http(provider.server_port, RUNS + "?check_name=" + NAME)
        assert code == 200 and visible["total_count"] == 1
        item = visible["check_runs"][0]
        assert (item["name"], item["head_sha"], item["status"], item["conclusion"]) == (
            NAME,
            HEAD,
            "completed",
            "neutral",
        )
        assert item["output"]["title"] == TITLE and plain(NOTE) in item["output"]["summary"]
        metadata = metadata_from(item["output"]["summary"])
        assert (
            metadata["slot"] == item["external_id"]
            and metadata["generation"] == initial_generation + 1
        )
        assert quorum.get("inbox/count") == "0"
        return {
            "etcd_version": voters.api(1, "maintenance/status", {})["version"],
            "initial_admission_http": 202,
            "standby_rediscovered_change": True,
            "real_primary_worker_killed": True,
            "original_private_root_detached": True,
            "open_pr": lab.CHANGE,
            "head_sha": HEAD,
            "worker_runs_on_standby": 4,
            "same_delivery_duplicate": True,
            "different_delivery_reused_completed_review": True,
            "model_calls": len(provider.model_packets),
            "native_check_creates": 1,
            "native_check_updates_for_identical_evidence": 0,
            "native_check_updates_after_ci_changed": 1,
            "externally_visible_reviews": visible["total_count"],
            "native_id": original["native_id"],
            "run_id": original["run_id"],
            "publication": original["publication"],
            "fixture_boundaries": [
                "Loopback provider served synthetic GitHub data, CI and App token exchange.",
                "The reviewer returned deterministic JSON. No model or Docker service ran.",
            ],
        }


def test_open_review_survives_primary_loss_and_worker_restart():
    binary = os.environ["CIRRA_TEST_ETCD"]
    with tempfile.TemporaryDirectory(prefix="cirra-open-review-") as root:
        evidence = accept_open_change(Path(binary).resolve(strict=True), Path(root))
    if path := os.environ.get("CIRRA_OPEN_REVIEW_EVIDENCE"):
        Path(path).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf8")


if __name__ == "__main__" and sys.argv[1:2] == ["_worker"]:
    raise SystemExit(fixture_worker(*sys.argv[2:]))
