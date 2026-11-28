import hashlib
import json
import os
import select
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection, HTTPResponse
from pathlib import Path
from types import SimpleNamespace

import pytest
from audit_fixtures import build_report
from test_audit_worker import AGENT, FakeGitHub, agent, request, store_baseline

from cirra import audit_generation, audit_read_snapshot, audit_reads, audits
from cirra.audit_reads import AuditReadServer
from cirra.audit_worker import AuditCheckPublisher, AuditPublication, audit_once
from cirra.cli import main as cli
from cirra.domain import CirraError, canonical, digest
from cirra.job_queue import AuditQueue
from cirra.publish import write_receipt
from cirra.store import Store

FINDING = ("C011", "app.py", 11, 12, "high")
NEW_FINDING = ("C012", "app.py", 13, 14, "critical")
READ_TOKEN = "audit-reader-fixture-" + "a" * 48
# The recorded fixture names the checkout generically instead of a host path.
RECORDED_AGENT = AGENT.replace(
    'repo_path=os.environ["CIRRA_AUDIT_CHECKOUT"]', 'repo_path="checkout"'
)


@pytest.fixture(autouse=True)
def reader_credential(monkeypatch):
    monkeypatch.delenv("CIRRA_AUDIT_READ_TOKEN_FILE", raising=False)
    monkeypatch.setenv("CIRRA_AUDIT_READ_TOKEN", READ_TOKEN)


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
    connection.request(method, path, headers={"Authorization": "Bearer " + READ_TOKEN})
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
    # The independent Taskmaster v1 consumer uses these literals.
    assert set(body) == {"protocol", "schema_version", "generation", "record", "sha256"}
    assert body["protocol"] == "cirra.audit-reads.v1" and body["schema_version"] == 3
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
        for query in ("limit=%C2%B2", "after=%C2%B2", "limit=" + "0" * 5000, "after=" + "0" * 19):
            assert get(empty, "/audits?" + query)[0].status == 400
        assert not (tmp_path / "empty").exists()
    finally:
        empty.shutdown()
        empty.server_close()
        thread.join(5)


def persisted_v1_generation(site):
    # The original wire digest is deliberately independent of the implementation.
    with site.store.connection() as db:
        rows = [
            list(r)
            for r in db.execute(
                "SELECT attempt_id, number, state, report_digest, created_at, completed_at, scope, "
                "comparison_json, validator_output FROM audits ORDER BY rowid"
            )
        ]
    receipts = []
    for name in ("audit-publication", "audit-waivers"):
        for path in sorted((site.server.state_dir / name).glob("*.json")):
            receipts.append([name, path.name, hashlib.sha256(path.read_bytes()).hexdigest()])
    raw = json.dumps([rows, receipts], sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def test_counter_skips_unrelated_commits_and_preserves_v1_generations(served, monkeypatch):
    state = served.server.state_dir
    scans = []
    scan = served.server.snapshot.rows

    def counted(db):
        scans.append(True)
        return scan(db)

    monkeypatch.setattr(served.server.snapshot, "rows", counted)

    def read():
        response, body = get(served.server, "/healthz")
        assert response.status == 200 and body["generation"] == persisted_v1_generation(served)
        return body["generation"]

    original = read()
    claim = served.store.acquire("counter-test", 60)
    assert read() == original and len(scans) == 2  # A plain v3 database still works.
    audit_generation.maintain(state, "install")
    assert read() == original and len(scans) == 3
    served.store.heartbeat(claim, 90)
    with served.store.transaction() as db:
        db.execute("UPDATE audits SET state=state")
    assert read() == original and len(scans) == 3
    attempt = begin_pr(served)
    prepared = read()
    assert prepared != original and len(scans) == 4
    complete(served, attempt)
    assert read() not in (prepared, original) and len(scans) == 5
    with pytest.raises(RuntimeError), served.store.transaction() as db:
        db.execute("DELETE FROM audits")
        raise RuntimeError
    read()
    assert len(scans) == 5
    with served.store.transaction() as db:
        db.execute("DELETE FROM audits WHERE attempt_id=?", (attempt["attempt_id"],))
    assert read() == original and len(scans) == 6
    audit_generation.maintain(state, "remove")
    assert read() == original and len(scans) == 7
    served.store.release(claim)
    assert read() == original and len(scans) == 8


def test_counter_reinstall_and_partial_metadata_never_reuse_stale_rows(served):
    state = served.server.state_dir
    audit_generation.maintain(state, "install")
    generation = get(served.server, "/healthz")[1]["generation"]
    audit_generation.maintain(state, "remove")
    begin_pr(served)
    audit_generation.maintain(state, "install")  # Both installations started at zero.
    current = get(served.server, "/healthz")[1]["generation"]
    assert current == persisted_v1_generation(served) != generation

    with served.store.transaction() as db:
        db.execute(f"DROP TRIGGER {audit_generation.TABLE}_insert")
    begin_pr(served)
    assert get(served.server, "/healthz")[1]["generation"] == persisted_v1_generation(served)
    for action in ("status", "install", "remove"):
        with pytest.raises(CirraError, match="incomplete or incompatible"):
            audit_generation.maintain(state, action)


def test_counter_detects_concurrent_commit_before_returning_a_record(served, monkeypatch):
    state = served.server.state_dir
    audit_generation.maintain(state, "install")
    original = get(served.server, "/healthz")[1]["generation"]
    index = served.server.index

    def mutate_after_query(connection, parsed):
        answer = index(connection, parsed)
        monkeypatch.setattr(served.server, "index", index)
        begin_pr(served)
        return answer

    monkeypatch.setattr(served.server, "index", mutate_after_query)
    response, body = get(served.server, "/audits?limit=200")
    assert response.status == 200 and len(body["record"]["audits"]) == 2
    assert body["generation"] == persisted_v1_generation(served) != original


def test_restored_copies_with_different_audits_at_the_same_count(served, tmp_path):
    state, original_path = served.server.state_dir, served.store.path
    audit_generation.maintain(state, "install")
    copy_path = tmp_path / "copy.sqlite3"
    with sqlite3.connect(original_path) as original, sqlite3.connect(copy_path) as copy:
        original.backup(copy)
        original.execute("UPDATE audits SET validator_output='first branch'")
        original.commit()
        before = get(served.server, "/healthz")[1]["generation"]
        copy.execute("UPDATE audits SET validator_output='restored branch'")
        copy.commit()
        assert audit_generation.counter(copy)[1] == audit_generation.counter(original)[1] == 1
        copy.backup(original)  # Restore in place while the same reader remains open.
    assert (
        get(served.server, "/healthz")[1]["generation"] == persisted_v1_generation(served) != before
    )


def test_read_and_status_do_not_install_or_write_the_counter(served):
    state, dbpath = served.server.state_dir, served.store.path
    for installed in (False, True):
        if installed:
            audit_generation.maintain(state, "install")
        source = dbpath.read_bytes()
        modified = dbpath.stat().st_mtime_ns
        response, _ = get(served.server, "/healthz")
        assert response.status == 200
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cirra",
                "audit-generation",
                "status",
                "--state-dir",
                str(state),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(result.stdout) == {
            "schema_version": 3,
            "audit_generation_installed": installed,
        }
        assert dbpath.read_bytes() == source and dbpath.stat().st_mtime_ns == modified


def test_counter_maintenance_never_creates_a_missing_or_foreign_store(tmp_path):
    for action in ("status", "install", "remove"):
        with pytest.raises(sqlite3.OperationalError):
            audit_generation.maintain(tmp_path, action)
    path = tmp_path / "cirra.sqlite3"
    assert not path.exists()
    store = Store(tmp_path)
    with store.transaction() as db:
        db.execute("PRAGMA user_version=4")
    source = path.read_bytes()
    for action in ("status", "install", "remove"):
        with pytest.raises(CirraError, match="existing v3"):
            audit_generation.maintain(tmp_path, action)
    assert path.read_bytes() == source
    with store.transaction() as db:
        db.execute("PRAGMA user_version=3")
        db.execute("ALTER TABLE audits RENAME COLUMN validator_output TO other")
    with pytest.raises(CirraError, match="schema 3 audit table"):
        audit_generation.maintain(tmp_path, "install")


def test_isolated_reader_cli_opens_audits_without_access_to_worker_metadata(tmp_path, monkeypatch):
    Store(tmp_path)

    def private_metadata(_):
        raise PermissionError("worker-queue-format.json")

    def healthy(server, **kwargs):
        status, response = server.read("/healthz")
        assert status == 200 and response["record"]["audits"] == 0
        raise KeyboardInterrupt

    monkeypatch.setattr("cirra.coordination.shared_state", private_metadata)
    monkeypatch.setattr(AuditReadServer, "serve_forever", healthy)
    assert cli(["serve-audit-reads", "--state-dir", str(tmp_path), "--port", "0"]) == 0


def test_generation_stays_v1_across_concurrent_replacement(served, monkeypatch):
    audit_id = "1" * 64
    path = served.server.state_dir / "audit-publication" / (audit_id + ".json")
    write_receipt(path, {"audit_id": audit_id, "gate": "block"})
    before = persisted_v1_generation(served)
    assert get(served.server, "/healthz")[1]["generation"] == before
    read = audit_read_snapshot.read_receipt

    def replace_during_gate(target):
        monkeypatch.setattr(audit_read_snapshot, "read_receipt", read)
        write_receipt(path, {"audit_id": audit_id, "gate": "pass"})
        return read(target)

    monkeypatch.setattr(audit_read_snapshot, "read_receipt", replace_during_gate)
    response, body = get(served.server, "/gates/" + audit_id)
    assert response.status == 200
    assert body["record"]["publication"]["gate"] == "pass"
    assert body["generation"] == persisted_v1_generation(served) != before


@pytest.mark.skipif(os.name == "nt", reason="POSIX permits replacing an open SQLite file")
def test_database_replacement_during_a_page_reopens_the_store(served, tmp_path, monkeypatch):
    assert get(served.server, "/healthz")[1]["record"]["audits"] == 1
    replacement = Store(tmp_path / "replacement")
    index = served.server.index

    def replace_after_query(connection, parsed):
        monkeypatch.setattr(served.server, "index", index)
        answer = index(connection, parsed)
        replacement.path.replace(served.store.path)
        return answer

    monkeypatch.setattr(served.server, "index", replace_after_query)
    response, body = get(served.server, "/audits")
    assert response.status == 200 and body["record"]["audits"] == []
    assert body["generation"] == persisted_v1_generation(served)


def test_receipt_scan_releases_source_database_and_warm_reads_reuse_it(served, monkeypatch):
    folder = served.server.state_dir / "audit-waivers"
    for i in range(200):
        write_receipt(folder / f"waiver-{i}.json", {"audit_id": f"{i:064x}"})
    entered, release = threading.Event(), threading.Event()
    scan = served.server.snapshot.refresh_directory

    def paused(name, revision):
        if name == "audit-waivers":
            entered.set()
            assert release.wait(4)
        return scan(name, revision)

    monkeypatch.setattr(served.server.snapshot, "refresh_directory", paused)
    with ThreadPoolExecutor(max_workers=2) as workers:
        reading = workers.submit(get, served.server, "/healthz")
        try:
            assert entered.wait(3)
            # A reader holding a rollback-journal read transaction across the
            # filesystem walk would block the writer's commit here.
            workers.submit(begin_pr, served).result(timeout=2)
        finally:
            release.set()
        assert reading.result()[0].status == 200

    def unexpected(*args):
        pytest.fail("a warm request rescanned audit history")

    monkeypatch.setattr(served.server.snapshot, "rows", unexpected)
    monkeypatch.setattr(served.server.snapshot, "refresh_directory", unexpected)
    monkeypatch.setattr(audit_read_snapshot, "read_receipt", unexpected)
    for route in ("/healthz", "/audits?limit=1"):
        assert get(served.server, route)[1]["generation"] == persisted_v1_generation(served)


def test_cold_index_continues_after_a_bounded_request(served, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    scan = served.server.snapshot.rows

    def paused(connection):
        entered.set()
        assert release.wait(4)
        return scan(connection)

    monkeypatch.setattr(served.server.snapshot, "rows", paused)
    monkeypatch.setattr(audit_read_snapshot, "READ_SECONDS", 0.15)
    try:
        response, _ = get(served.server, "/healthz")
        assert entered.is_set() and response.status == 503
    finally:
        release.set()
    served.server.snapshot.build.result(timeout=3)
    assert get(served.server, "/healthz")[0].status == 200


def test_summary_page_memory_does_not_include_large_reports(served):
    with served.store.transaction() as db:
        db.executemany(
            "INSERT INTO audits(attempt_id,audit_id,number,repository_key,tier,head,state,"
            "identity_json,created_at,scope,report_json) VALUES(?,?,1,'r','pr','h','complete',"
            "'{}',0,'repository',zeroblob(1048576))",
            [(str(i), str(i)) for i in range(32)],
        )
    tracemalloc.start()
    try:
        response, body = get(served.server, "/audits?limit=50")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert response.status == 200 and len(body["record"]["audits"]) == 33
    assert peak < 6 * 1024 * 1024


@pytest.fixture
def access_log(monkeypatch):
    records, completed = [], threading.Event()
    write_log = audit_reads.log_line

    def capture(record):
        write_log(record)
        records.append(record)
        if record["event"] == "audit_read":
            completed.set()

    monkeypatch.setattr(audit_reads, "log_line", capture)
    return records, completed


def test_read_error_is_correlated_without_echoing_input(served, access_log, capsys):
    (served.server.state_dir / "audit-waivers").mkdir()
    (served.server.state_dir / "audit-waivers" / "waiver.json").write_text(
        "secret-body", encoding="utf8"
    )
    response, _ = get(served.server, "/audits?tier=private")
    assert response.status == 503
    _, completed = access_log
    assert completed.wait(2)
    records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    error, access = [
        row for row in records if row["request_id"] == response.getheader("X-Request-ID")
    ]
    assert error["level"] == "error" and error["operation"] == "audit_read"
    assert error["route"] == access["route"] == "audits"
    assert error["request_id"] == response.getheader("X-Request-ID") == access["request_id"]
    assert access["outcome"] == "completed" and access["status"] == 503
    assert "secret-body" not in str(records) and "private" not in str(records)


@pytest.mark.parametrize("disconnect", [False, True])
def test_completion_includes_body_write_and_client_disconnect(
    served, access_log, monkeypatch, disconnect
):
    records, completed = access_log
    headers_sent, release = threading.Event(), threading.Event()
    peers = []
    clock = [10.0]
    monkeypatch.setattr(audit_reads, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    end_headers = audit_reads.ReadHandler.end_headers

    def pause_before_body(handler):
        end_headers(handler)
        peers.append(handler.request)
        headers_sent.set()
        assert release.wait(4)

    monkeypatch.setattr(audit_reads.ReadHandler, "end_headers", pause_before_body)
    with socket.create_connection(served.server.server_address, timeout=4) as client:
        if disconnect:
            # Force an observable peer reset on both Winsock and POSIX.
            linger = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        client.sendall(
            (
                "GET /audits?tier=private HTTP/1.0\r\nAuthorization: Bearer "
                + READ_TOKEN
                + "\r\n\r\n"
            ).encode()
        )
        with HTTPResponse(client) as response:
            response.begin()
            request_id = response.getheader("X-Request-ID")
            try:
                assert headers_sent.wait(2) and response.status == 200
                assert records == []
                if disconnect:
                    response.close()
                    client.close()
                    assert select.select(peers, [], [], 2)[0]
                clock[0] += 2.75
            finally:
                release.set()
            if not disconnect:
                assert json.loads(response.read())["record"]["audits"] == []
        assert completed.wait(2)
    [entry] = records
    assert entry["request_id"] == request_id
    assert entry["duration_ms"] == 2750 and entry["status"] == 200
    assert entry["outcome"] == ("disconnected" if disconnect else "completed")
    assert entry["level"] == ("warning" if disconnect else "info")
    assert entry["route"] == "audits" and "private" not in str(entry)


def test_cli_serves_reads_without_publisher_credentials(world):
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
    connection.request("GET", path, headers={"Authorization": "Bearer " + READ_TOKEN})
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
