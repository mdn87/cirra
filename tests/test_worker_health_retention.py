"""Queue admission during rollout and cold retention of audit-agent output."""

import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace

import pytest
from audit_fixtures import audit_policy

from cirra import audits, retention
from cirra.domain import digest, utc_now
from cirra.health import check
from cirra.job_queue import Queue
from cirra.retention import cold_evidence
from cirra.store import Store
from cirra.worker import heartbeat


def test_health_reports_legacy_census_until_new_admission_resumes(tmp_path):
    queue = Queue(tmp_path, maximum=64)
    route = SimpleNamespace(name="repo", digest="policy-1")
    stamp = utc_now()
    hint = {"change": 3, "delivery": "old", "payload_digest": "old"}
    for number in range(40):
        value = str(number) if number else "old"
        identifier = digest([route.name, value])
        job = {
            "id": identifier,
            "route": route.name,
            "route_digest": route.digest,
            "state": "done",
            "attempts": 1,
            "not_before": 0,
            "accepted_at": stamp,
            "updated_at": stamp,
            "hint": {**hint, "delivery": value, "payload_digest": value},
        }
        (queue.directory / f"{identifier}.json").write_text(json.dumps(job), encoding="utf-8")
    heartbeat(queue)
    cold = check(tmp_path, include_metrics=True)
    assert cold["healthy"] is False
    assert cold["queue_metrics"]["census_pending"] is True
    assert cold["queue_metrics"]["remaining"] == 0
    assert any(p["kind"] == "capacity" and "counting" in p["detail"] for p in cold["problems"])
    assert queue.enqueue(route, hint)["duplicate"]

    assert queue.maintain()
    warm = check(tmp_path, include_metrics=True)
    assert warm["healthy"] is True
    assert warm["queue_metrics"]["census_pending"] is False
    assert warm["queue_metrics"]["remaining"] == 64
    assert not queue.enqueue(route, {**hint, "delivery": "new", "payload_digest": "new"})[
        "duplicate"
    ]


@pytest.fixture
def audit_logs(tmp_path, policy_data):
    config = audit_policy(tmp_path, policy_data)
    state = tmp_path / "state"
    store = Store(state)
    repository = audits.select_repository(config)
    now = time.time()
    old = now - 31 * 86400

    def sample(number, status, *, completed=old, log_time=old, dir_time=old, checkout=False):
        identity = audits.audit_identity(config.audit, "full", repository, f"{number:040x}")
        attempt, _ = audits.begin(store, identity)
        identifier = attempt["attempt_id"]
        with store.transaction() as connection:
            connection.execute(
                "UPDATE audits SET state=?, completed_at=? WHERE attempt_id=?",
                (status, completed, identifier),
            )
        directory = state / "audit-attempts" / identifier
        output = directory / "work"
        output.mkdir(parents=True)
        if checkout:
            (directory / "checkout").mkdir()
        log = output / "agent.log"
        log.write_text("saved output", encoding="utf-8")
        checkpoint = output / "checkpoint.json"
        checkpoint.write_text("{}", encoding="utf-8")
        for path, stamp in ((log, log_time), (checkpoint, old), (directory, dir_time)):
            os.utime(path, (stamp, stamp))
        return log.relative_to(state)

    return state, store, now, sample


def test_cold_manifest_only_lists_old_closed_audit_agent_logs(audit_logs):
    state, _, now, sample = audit_logs
    selected = {
        sample(1, "complete"),
        sample(2, "cancelled", completed=None),
    }
    sample(3, "prepared", completed=None)
    sample(4, "superseded", completed=None, checkout=True)
    sample(5, "complete", completed=now)
    sample(6, "failed", log_time=now)
    sample(7, "complete", dir_time=now)
    assert set(cold_evidence(state, now=now)) == selected


def test_manifest_releases_database_while_paging_closed_logs(audit_logs, monkeypatch):
    state, store, now, sample = audit_logs
    selected = {sample(1, "complete"), sample(2, "cancelled", completed=None)}
    sample(3, "prepared", completed=None)
    sample(4, "prepared", completed=None)
    selected.update((sample(5, "failed"), sample(6, "superseded", completed=None)))
    monkeypatch.setattr(retention, "AUDIT_PAGE_SIZE", 2)
    connect = sqlite3.connect
    readers = []

    def connect_reader(*args, **kwargs):
        connection = connect(*args, **kwargs)
        if kwargs.get("uri"):
            readers.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect_reader)

    def write_while_manifest_paused(identifier):
        with closing(connect(store.path, timeout=0.3)) as writer, writer:
            assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            writer.execute(
                "UPDATE audits SET validator_output='checked' WHERE attempt_id=?", (identifier,)
            )

    with closing(cold_evidence(state, now=now)) as manifest:
        first = next(manifest)
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(write_while_manifest_paused, first.parts[1]).result(timeout=5)
        for reader in readers:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                reader.execute("SELECT 1")
        paths = [first, *manifest]
    assert len(readers) >= 3
    assert len(paths) == len(selected) and set(paths) == selected
    assert audits.get(store, first.parts[1])["validator_output"] == "checked"
