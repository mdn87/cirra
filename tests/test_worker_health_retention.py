"""Queue admission during rollout and cold retention of audit-agent output."""

import json
import os
import time
from types import SimpleNamespace

from audit_fixtures import audit_policy

from cirra import audits
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


def test_cold_manifest_only_lists_old_closed_audit_agent_logs(tmp_path, policy_data):
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
