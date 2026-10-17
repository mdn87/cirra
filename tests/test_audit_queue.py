"""Stage 3 of the production-audit tiers plan: work keys, coalescing and fencing."""

import json

import pytest
from audit_fixtures import audit_policy

from cirra import audits
from cirra.audit_worker import audit_once
from cirra.domain import CirraError
from cirra.job_queue import AuditQueue
from cirra.store import Store

T0 = 1_800_000_000.0
DAY = 86400


def head(number):
    return f"{number:040x}"


@pytest.fixture
def env(tmp_path, policy_data):
    config = audit_policy(tmp_path, policy_data)
    store = Store(tmp_path / "state")
    queue = AuditQueue(tmp_path / "state", store)
    repository = audits.select_repository(config)

    def identity(tier, revision, base=None):
        return audits.audit_identity(config.audit, tier, repository, revision, base)

    def pr(number, revision, now, debounce=120):
        return queue.request(
            identity("pr", revision, head(999)),
            f"change:{number}",
            reason="change",
            debounce_seconds=debounce,
            now=now,
        )

    return queue, store, identity, pr


def session(job, attempt, stop):
    return {"state": "awaiting_import"}


def states(queue):
    return [(job["identity"]["head"], job["state"]) for job in queue.jobs()]


def test_burst_of_pushes_runs_one_audit_of_the_last_head(env):
    queue, store, _, pr = env
    for offset, revision in enumerate((1, 2, 3)):
        pr(1, head(revision), T0 + offset * 10)
    assert states(queue) == [
        (head(1), "superseded"),
        (head(2), "superseded"),
        (head(3), "queued"),
    ]
    # Debounce: nothing is runnable until the last push has been quiet long enough.
    assert audit_once(queue, store, 20, session, now=T0 + 30) is None
    job = audit_once(queue, store, 20, session, now=T0 + 20 + 121)
    assert job["state"] == "awaiting_import" and job["identity"]["head"] == head(3)
    assert audit_once(queue, store, 20, session, now=T0 + 1000) is None
    with store.connection() as connection:
        rows = connection.execute("SELECT head FROM audits").fetchall()
    assert [row["head"] for row in rows] == [head(3)]


def test_push_during_running_audit_supersedes_it_and_refuses_the_late_import(env, tmp_path):
    queue, store, _, pr = env
    pr(1, head(1), T0, debounce=0)

    def runner(job, attempt, stop):
        pr(1, head(2), T0 + 5, debounce=0)  # A push lands while the agent works.
        return {"state": "stopped", "reason": stop()}

    old = audit_once(queue, store, 20, runner, now=T0 + 1)
    assert old["state"] == "superseded" and old["superseded_by"]
    assert audits.get(store, old["attempt_id"])["state"] == "superseded"
    late = tmp_path / "late.json"
    late.write_text(json.dumps({"tier": "pr"}), encoding="utf-8")
    with pytest.raises(CirraError, match="attempt is superseded"):
        audits.import_report(store, old["attempt_id"], late, tmp_path)
    new = audit_once(queue, store, 20, session, now=T0 + 6)
    assert new["identity"]["head"] == head(2) and new["state"] == "awaiting_import"


def test_supersession_fences_a_session_audit_awaiting_import(env, tmp_path):
    queue, store, _, pr = env
    pr(1, head(1), T0, debounce=0)
    waiting = audit_once(queue, store, 20, session, now=T0 + 1)
    pr(1, head(2), T0 + 2, debounce=0)
    assert audits.get(store, waiting["attempt_id"])["state"] == "superseded"
    audit_once(queue, store, 20, session, now=T0 + 3)
    assert queue.read(waiting["id"])["state"] == "superseded"


def test_duplicate_delivery_attaches_without_new_work(env):
    queue, store, identity, pr = env
    first = pr(1, head(1), T0, debounce=0)
    again = pr(1, head(1), T0 + 1, debounce=0)
    assert again == {"job_id": first["job_id"], "attached": True}
    assert len(queue.jobs()) == 1
    # A completed identity also attaches, even when only the store knows it.
    attempt, _ = audits.begin(store, identity("full", head(5)))
    with store.transaction() as connection:
        connection.execute(
            "UPDATE audits SET state='complete', completed_at=? WHERE attempt_id=?",
            (T0, attempt["attempt_id"]),
        )
    attached = queue.request(identity("full", head(5)), "repository", reason="schedule")
    assert attached == {"job_id": None, "attempt_id": attempt["attempt_id"], "attached": True}
    assert len(queue.jobs()) == 1


def test_release_request_is_never_superseded_by_pull_request_traffic(env):
    queue, store, identity, pr = env
    release = queue.request(identity("release", head(7)), "release:main", reason="release", now=T0)
    for number, revision in ((1, 1), (2, 2), (1, 3)):
        pr(number, head(revision), T0 + revision, debounce=0)
    assert queue.read(release["job_id"])["state"] == "queued"
    first = audit_once(queue, store, 20, session, now=T0 + 10)
    assert first["id"] == release["job_id"]
    # A new commit on the same release ref does supersede the older release request.
    newer = queue.request(identity("release", head(8)), "release:main", reason="release")
    assert queue.read(release["job_id"])["superseded_by"] == newer["job_id"]


def test_priority_is_release_then_pr_then_full(env):
    queue, store, identity, pr = env
    queue.request(identity("full", head(1)), "repository", reason="schedule", now=T0)
    pr(1, head(2), T0, debounce=0)
    queue.request(identity("release", head(3)), "release:main", reason="release", now=T0)
    order = [audit_once(queue, store, 20, session, now=T0 + 1)["tier"] for _ in range(3)]
    assert order == ["release", "pr", "full"]


def test_cancellation_is_final_and_fenced(env, tmp_path):
    queue, store, identity, pr = env
    queued = pr(1, head(1), T0)
    assert queue.cancel(queued["job_id"], "not needed")["state"] == "cancelled"
    with pytest.raises(CirraError, match="already cancelled"):
        queue.cancel(queued["job_id"], "again")

    queue.request(identity("full", head(2)), "repository", reason="baseline", now=T0)

    def runner(job, attempt, stop):
        queue.cancel(job["id"], "operator stop")
        # Fenced at once, before the runner reaches its next checkpoint.
        assert audits.get(store, attempt["attempt_id"])["state"] == "cancelled"
        return {"state": "stopped", "reason": stop()}

    job = audit_once(queue, store, 20, runner, now=T0 + 1)
    assert job["state"] == "cancelled"
    assert audits.get(store, job["attempt_id"])["state"] == "cancelled"
    late = tmp_path / "late.json"
    late.write_text("{}", encoding="utf-8")
    with pytest.raises(CirraError, match="attempt is cancelled"):
        audits.import_report(store, job["attempt_id"], late, tmp_path)
    # Cancelled work is never retried.
    assert audit_once(queue, store, 20, session, now=T0 + 2) is None


def test_daily_admission_defers_gating_tiers_and_holds_full(env):
    queue, store, identity, pr = env
    pr(1, head(1), T0, debounce=0)
    pr(2, head(2), T0, debounce=0)
    queue.request(identity("full", head(3)), "repository", reason="schedule", now=T0)
    assert audit_once(queue, store, 1, session, now=T0 + 1)["identity"]["head"] == head(1)
    assert audit_once(queue, store, 1, session, now=T0 + 2) is None
    by_head = {job["identity"]["head"]: job["state"] for job in queue.jobs()}
    assert by_head == {head(1): "awaiting_import", head(2): "deferred", head(3): "queued"}
    tomorrow = T0 + DAY
    assert audit_once(queue, store, 1, session, now=tomorrow)["identity"]["head"] == head(2)
    assert audit_once(queue, store, 1, session, now=tomorrow + 1) is None


def test_runner_failure_fails_the_job_and_fences_the_attempt(env):
    queue, store, _, pr = env
    pr(1, head(1), T0, debounce=0)

    def broken(job, attempt, stop):
        raise CirraError("agent exited with status 3")

    job = audit_once(queue, store, 20, broken, now=T0 + 1)
    assert job["state"] == "failed" and job["error"] == "agent exited with status 3"
    assert audits.get(store, job["attempt_id"])["state"] == "cancelled"


def test_operator_commands_request_list_and_cancel(tmp_path, policy_data, capsys):
    from cirra.cli import main

    config = audit_policy(tmp_path, policy_data)
    state = ["--state-dir", str(tmp_path / "cli")]
    request = ["request-audit", "--config", config.path, "--tier", "release", "--head", head(1)]
    assert main([*request, "--ref", "main", *state]) == 0
    job = json.loads(capsys.readouterr().out)
    assert main([*request, "--ref", "main", *state]) == 0
    assert json.loads(capsys.readouterr().out) == {"job_id": job["job_id"], "attached": True}
    assert main([*request, *state]) == 2
    assert "release and full requests need --ref" in capsys.readouterr().err
    assert main(["audit-jobs", *state]) == 0
    [listed] = json.loads(capsys.readouterr().out)
    assert listed["scope"] == "release:main" and listed["reason"] == "operator"
    assert main(["cancel-audit", "--job", job["job_id"], "--reason", "test", *state]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "cancelled"
