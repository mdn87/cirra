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


@pytest.mark.parametrize("backup_age", [None, 24 * 3600])
def test_active_coordinated_host_reports_missing_or_stale_backup(tmp_path, backup_age):
    from datetime import UTC, datetime, timedelta

    from cirra.publish import write_receipt

    queue = Queue(tmp_path)
    heartbeat(queue)
    now = datetime.now(UTC)
    authority = {
        "host": "1" * 64,
        "epoch": "2" * 64,
        "tip": "3" * 64,
        "session": "4" * 64,
        "floor": int(now.timestamp()) - 7200,
        "target": None,
    }
    write_receipt(tmp_path / "coordination-state.json", {"cluster": "fixture"})
    write_receipt(tmp_path / "data-authority.json", authority)
    if backup_age is not None:
        write_receipt(
            tmp_path / "data-backup.json",
            {
                **{k: authority[k] for k in ("host", "epoch", "tip")},
                "snapshot_id": "5" * 64,
                "captured_at": (now - timedelta(seconds=backup_age)).isoformat(),
            },
        )
    assert any(p["kind"] == "backup" for p in check(tmp_path, now=now)["problems"])


def test_archived_unresolved_failure_remains_visible_but_requeue_resolves_it(tmp_path):
    from datetime import UTC, datetime, timedelta

    queue = Queue(tmp_path)
    route = SimpleNamespace(name="repo", digest="policy-1")
    identifier = queue.enqueue(route, {"change": 3, "delivery": "old", "payload_digest": "old"})[
        "job_id"
    ]
    path = queue.directory / (identifier + ".json")
    job = queue.review_record(path)
    job.update(state="attention", updated_at=(datetime.now(UTC) - timedelta(days=10)).isoformat())
    queue.save(job)
    queue.archive_finished([job])
    assert path.exists()  # New maintenance must never hide unresolved work.
    from cirra.health import queue_metrics

    assert queue_metrics(queue)["remaining"] == queue.maximum - 1
    archive = queue.directory / "archive"
    archive.mkdir(exist_ok=True)
    path.replace(archive / path.name)  # Reproduce a pre-upgrade archived failure.
    heartbeat(queue)
    assert any(p["kind"] == "attention" for p in check(tmp_path)["problems"])
    job.update(state="queued", updated_at=datetime.now(UTC).isoformat())
    queue.save(job)
    assert not any(p["kind"] == "attention" for p in check(tmp_path)["problems"])


def test_new_authority_gets_one_hour_to_replace_its_donors_backup(tmp_path):
    from datetime import UTC, datetime, timedelta

    from cirra.publish import write_receipt

    now = datetime.now(UTC)
    queue = Queue(tmp_path)
    heartbeat(queue)
    authority = {
        "host": "1" * 64,
        "epoch": "2" * 64,
        "tip": "3" * 64,
        "session": "4" * 64,
        "floor": int(now.timestamp()),
        "target": None,
    }
    write_receipt(tmp_path / "coordination-state.json", {"cluster": "fixture"})
    write_receipt(tmp_path / "data-authority.json", authority)
    backup = {
        "host": "a" * 64,
        "epoch": "b" * 64,
        "tip": "c" * 64,
        "snapshot_id": "d" * 64,
        "captured_at": now.isoformat(),
    }
    write_receipt(tmp_path / "data-backup.json", backup)
    assert not any(p["kind"] == "backup" for p in check(tmp_path, now=now)["problems"])
    assert any(
        p["kind"] == "backup" for p in check(tmp_path, now=now + timedelta(hours=2))["problems"]
    )
    backup.update({k: authority[k] for k in ("host", "epoch")})
    write_receipt(tmp_path / "data-backup.json", backup)
    assert not any(
        p["kind"] == "backup" for p in check(tmp_path, now=now + timedelta(hours=2))["problems"]
    )


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
