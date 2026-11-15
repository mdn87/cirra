import json
import os
import subprocess
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest
from audit_fixtures import build_report
from test_audit_worker import AGENT, FakeGitHub, agent, request, store_baseline

from cirra import audits
from cirra.audit_reads import PROTOCOL, AuditReadServer
from cirra.audit_worker import AuditCheckPublisher, AuditPublication, audit_once
from cirra.domain import CirraError, canonical, digest
from cirra.job_queue import AuditQueue
from cirra.publish import write_receipt
from cirra.store import Store

FINDING = ("C011", "app.py", 11, 12, "high")
NEW_FINDING = ("C012", "app.py", 13, 14, "critical")
# The recorded fixture names the checkout generically instead of a host path.
RECORDED_AGENT = AGENT.replace(
    'repo_path=os.environ["CIRRA_AUDIT_CHECKOUT"]', 'repo_path="checkout"'
)


@pytest.fixture
def served(world):
    route, _ = world.make()
    store_baseline(world, route, gaps=(FINDING,))
    server = AuditReadServer(("127.0.0.1", 0), world.state)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield SimpleNamespace(server=server, route=route, store=Store(world.state), world=world)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def get(server, path, method="GET"):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    connection.request(method, path)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response, json.loads(body) if body else None


def begin_pr(site):
    repository = audits.select_repository(site.route.policy)
    identity = audits.audit_identity(
        site.route.policy.audit, "pr", repository, site.world.head, site.world.base
    )
    attempt, _ = audits.begin(site.store, identity, "change:7", fresh=True)
    return attempt


def complete(site, attempt, gaps=(FINDING,), comparison=None):
    """Store a complete report for a prepared attempt, as the worker does."""
    report = build_report(attempt["tier"], attempt["head"], site.world.base, gaps=gaps)
    with site.store.transaction() as connection:
        connection.execute(
            "UPDATE audits SET state='complete', completed_at=?, report_json=?, "
            "report_digest=?, comparison_json=? WHERE attempt_id=?",
            (
                time.time(),
                canonical(report),
                digest(report),
                canonical(comparison or {"new": [], "open": [], "resolved": []}),
                attempt["attempt_id"],
            ),
        )


def test_only_get_is_served(served):
    for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
        response, body = get(served.server, "/healthz", method)
        assert response.status == 405, method
        assert response.getheader("Allow") == "GET"
        assert body is None or body == {"error": "method not allowed", "allow": ["GET"]}
    response, body = get(served.server, "/healthz")
    assert response.status == 200
    assert response.getheader("Cache-Control") == "no-store"
    assert body["protocol"] == PROTOCOL and body["schema_version"] == 3
    assert body["record"]["status"] == "ok" and body["record"]["audits"] == 1


def test_refuses_any_bind_but_loopback(world):
    for host in ("0.0.0.0", "10.0.0.5", "::1", "localhost"):
        with pytest.raises(CirraError, match="127.0.0.1"):
            AuditReadServer((host, 0), world.state)


def test_record_hash_and_generation_are_stable_for_an_unchanged_store(served):
    index = get(served.server, "/audits")[1]
    [summary] = index["record"]["audits"]
    assert summary["tier"] == "full" and summary["head"] == served.world.base
    assert summary["completed_at"].endswith("+00:00")
    first = get(served.server, f"/audits/{summary['attempt_id']}")
    second = get(served.server, f"/audits/{summary['attempt_id']}")
    for response, body in (first, second):
        assert response.status == 200
        assert body["sha256"] == digest(body["record"])
        assert body["generation"] == index["generation"]
        assert body["record"]["report"]["findings"][0]["path"] == "app.py"
        assert body["record"]["comparison"] == {"new": [], "open": [], "resolved": []}
    assert first[1] == second[1]
    assert get(served.server, "/healthz")[1]["generation"] == index["generation"]


def test_a_changed_store_changes_the_generation(served):
    generations = [get(served.server, "/healthz")[1]["generation"]]
    attempt = begin_pr(served)
    generations.append(get(served.server, "/healthz")[1]["generation"])
    complete(served, attempt)
    generations.append(get(served.server, "/healthz")[1]["generation"])
    write_receipt(
        served.server.state_dir / "audit-publication" / f"{attempt['audit_id']}.json",
        {"state": "published", "audit_id": attempt["audit_id"], "gate": "pass"},
    )
    generations.append(get(served.server, "/healthz")[1]["generation"])
    assert len(set(generations)) == 4
    assert get(served.server, "/healthz")[1]["generation"] == generations[-1]


def test_gate_is_the_stored_receipt_never_recomputed(served):
    attempt = begin_pr(served)
    complete(served, attempt)
    assert get(served.server, f"/gates/{attempt['audit_id']}")[0].status == 404
    publication = {
        "state": "published",
        "audit_id": attempt["audit_id"],
        "attempt_id": attempt["attempt_id"],
        "gate": "block",
        "conclusion": "failure",
        "blocking": 2,
        "native_id": 99,
        "existing": False,
    }
    waiver = {
        "request_id": "r1",
        "audit_id": attempt["audit_id"],
        "attempt_id": attempt["attempt_id"],
        "level": "critical",
        "gate": "block",
        "blocking": ["C011:app.py"],
        "state": "waived",
        "reason": "Accepted risk",
        "waived_at": "2026-10-19T10:00:00+00:00",
    }
    write_receipt(
        served.server.state_dir / "audit-publication" / f"{attempt['audit_id']}.json", publication
    )
    write_receipt(served.server.state_dir / "audit-waivers" / "r1.json", waiver)
    write_receipt(
        served.server.state_dir / "audit-waivers" / "r2.json", {**waiver, "audit_id": "0" * 64}
    )
    response, body = get(served.server, f"/gates/{attempt['audit_id']}")
    assert response.status == 200
    assert body["record"] == {
        "audit_id": attempt["audit_id"],
        "publication": publication,
        "waivers": [waiver],
    }
    assert body["sha256"] == digest(body["record"])


def test_index_is_bounded_filtered_and_strict(served):
    attempts = []
    for _ in range(3):
        attempts.append(begin_pr(served))
        complete(served, attempts[-1])
    page = get(served.server, "/audits?tier=pr&limit=2")[1]["record"]
    assert [row["attempt_id"] for row in page["audits"]] == [a["attempt_id"] for a in attempts[:2]]
    assert page["next"] == page["audits"][-1]["sequence"]
    rest = get(served.server, f"/audits?tier=pr&limit=2&after={page['next']}")[1]["record"]
    assert [row["attempt_id"] for row in rest["audits"]] == [attempts[2]["attempt_id"]]
    assert rest["next"] is None
    by_head = get(served.server, f"/audits?head={served.world.base}")[1]["record"]["audits"]
    assert [row["tier"] for row in by_head] == ["full"]
    for query in ("limit=0", "limit=201", "after=x", "tier=PR", "owner=1", "tier=pr&tier=pr"):
        assert get(served.server, f"/audits?{query}")[0].status == 400, query
    assert get(served.server, "/healthz?x=1")[0].status == 400


def test_unknown_paths_missing_store_and_packets(served, tmp_path):
    for path in ("/", "/audits/not-hex", "/gates/abc", "/audit-packets/x.json", "/audits/"):
        assert get(served.server, path)[0].status == 404, path
    empty = AuditReadServer(("127.0.0.1", 0), tmp_path / "empty")
    thread = threading.Thread(target=empty.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        response, body = get(empty, "/healthz")
        assert response.status == 503 and body == {"error": "audit store unavailable"}
        assert not (tmp_path / "empty").exists()
    finally:
        empty.shutdown()
        empty.server_close()
        thread.join(5)


def test_cli_serves_reads_without_credentials(world):
    route, _ = world.make()
    store_baseline(world, route)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cirra",
            "serve-audit-reads",
            "--state-dir",
            str(world.state),
            "--port",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": "src"},
        text=True,
    )
    try:
        banner = json.loads(process.stdout.readline())
        port = int(banner["listening"].rsplit(":", 1)[1])
        assert banner["routes"] == [
            "/healthz",
            "/audits",
            "/audits/<attempt_id>",
            "/gates/<audit_id>",
        ]
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/healthz")
        assert json.loads(connection.getresponse().read())["record"]["audits"] == 1
    finally:
        process.terminate()
        process.wait(5)
    help_text = subprocess.run(
        [sys.executable, "-m", "cirra", "serve-audit-reads", "--help"],
        capture_output=True,
        env={**os.environ, "PYTHONPATH": "src"},
        text=True,
    ).stdout
    assert "--service-config" not in help_text and "--env-file" not in help_text


def fetch(server, path):
    response, _ = get(server, path)
    assert response.status == 200, path
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    connection.request("GET", path)
    body = connection.getresponse().read()
    connection.close()
    return body


def test_pipeline_pr_check_over_baseline_is_served(world, tmp_path):
    """A real cirra/audit PR check over a full baseline, read back through the route.

    With CIRRA_RECORD_AUDIT_READS=<dir> the 200 bodies are also written there
    byte for byte: Taskmaster's saved-snapshot fixture (docs/service-workflow.md).
    """
    assert RECORDED_AGENT != AGENT
    route, runner = world.make(agent(tmp_path, [FINDING, NEW_FINDING], RECORDED_AGENT))
    store_baseline(world, route, gaps=(FINDING,))
    request(world, route, "app.py")
    store = Store(world.state)
    github = FakeGitHub()
    publication = AuditPublication(
        world.state,
        {"main": route},
        lambda r: AuditCheckPublisher(r.repository, 5, "t", http=github),
    )
    job = audit_once(AuditQueue(world.state, store), store, 20, runner, on_complete=publication)
    assert job["state"] == "done", json.dumps(job)[:4000]
    assert [post["name"] for post in github.posts] == ["cirra/audit"]
    server = AuditReadServer(("127.0.0.1", 0), world.state)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        bodies = {"healthz.json": fetch(server, "/healthz")}
        rows = {}
        for head in (world.head, world.base):
            bodies[f"audits-by-head/{head}.json"] = fetch(server, f"/audits?head={head}&limit=200")
            [rows[head]] = json.loads(bodies[f"audits-by-head/{head}.json"])["record"]["audits"]
        pr, full = rows[world.head], rows[world.base]
        assert (pr["tier"], pr["attempt_id"], full["tier"]) == ("pr", job["attempt_id"], "full")
        for row in (pr, full):
            bodies[f"audits/{row['attempt_id']}.json"] = fetch(
                server, f"/audits/{row['attempt_id']}"
            )
        bodies[f"gates/{pr['audit_id']}.json"] = fetch(server, f"/gates/{pr['audit_id']}")
        assert get(server, f"/gates/{full['audit_id']}")[0].status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    records = {name: json.loads(body) for name, body in bodies.items()}
    assert len({doc["generation"] for doc in records.values()}) == 1
    assert all(doc["sha256"] == digest(doc["record"]) for doc in records.values())
    detail = records[f"audits/{pr['attempt_id']}.json"]["record"]
    assert detail["comparison"]["new"] == ["C012:app.py"]
    assert detail["comparison"]["open"] == ["C011:app.py"]
    gate = records[f"gates/{pr['audit_id']}.json"]["record"]
    assert gate["waivers"] == []
    assert gate["publication"]["attempt_id"] == pr["attempt_id"]
    assert (gate["publication"]["gate"], gate["publication"]["blocking"]) == ("block", 1)
    target = os.environ.get("CIRRA_RECORD_AUDIT_READS")
    if target:
        for name, body in bodies.items():
            path = Path(target) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
