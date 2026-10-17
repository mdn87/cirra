"""Regression tests for the review findings on the audit queue and worker (#17, #18)."""

import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from audit_fixtures import audit_policy, build_report
from test_audit_worker import FakeGitHub, agent, request, store_baseline

from cirra import audit_worker, audits, job_queue
from cirra.audit_worker import AuditCheckPublisher, AuditPublication, audit_once
from cirra.domain import CirraError
from cirra.job_queue import AuditQueue
from cirra.store import Store


def head(number):
    return f"{number:040x}"


@pytest.fixture
def env(tmp_path, policy_data):
    config = audit_policy(tmp_path, policy_data)
    store = Store(tmp_path / "state")
    queue = AuditQueue(tmp_path / "state", store)
    repository = audits.select_repository(config)

    def pr(revision, change=1):
        identity = audits.audit_identity(config.audit, "pr", repository, head(revision), head(99))
        return queue.request(identity, f"change:{change}", reason="change")

    return queue, store, config, pr


def session(job, attempt, stop):
    return {"state": "awaiting_import"}


def test_orphaned_running_job_is_requeued_and_runs_after_a_restart(env):
    queue, store, _, pr = env
    pr(1)
    claimed = queue.next_job(20)  # The worker died right after claiming it.
    assert claimed["state"] == "running"
    job = audit_once(queue, store, 20, session)
    assert job["id"] == claimed["id"] and job["state"] == "awaiting_import"
    assert job["recovered"] == 1


def test_running_job_with_an_imported_report_settles_after_a_restart(env):
    queue, store, _, pr = env
    pr(1)
    claimed = queue.next_job(20)
    attempt, _ = audits.begin(store, claimed["identity"], claimed["scope"])
    queue.update(claimed["id"], attempt_id=attempt["attempt_id"])
    with store.transaction() as connection:  # imported, then the worker died
        connection.execute(
            "UPDATE audits SET state='complete', completed_at=?, report_json=?, "
            "comparison_json=? WHERE attempt_id=?",
            (
                time.time(),
                json.dumps(build_report("pr", head(1), head(99))),
                json.dumps({"new": [], "open": [], "resolved": []}),
                attempt["attempt_id"],
            ),
        )
    published = []
    assert (
        audit_once(
            queue,
            store,
            20,
            session,
            on_complete=lambda j, a: published.append(1) or {"state": "published"},
        )
        is None
    )
    assert queue.read(claimed["id"])["state"] == "done" and published == [1]


def test_cancellation_before_the_attempt_exists_is_applied_before_running(env, monkeypatch):
    queue, store, _, pr = env
    pr(1)
    claim = queue.next_job

    def claim_then_cancel(maximum, now=None):
        job = claim(maximum, now)
        if job:
            queue.cancel(job["id"], "operator stop")  # no attempt to fence yet
        return job

    monkeypatch.setattr(queue, "next_job", claim_then_cancel)
    ran = []
    job = audit_once(queue, store, 20, lambda *args: ran.append(1))
    assert job["state"] == "cancelled" and ran == []
    assert audits.get(store, job["attempt_id"])["state"] == "cancelled"


@pytest.mark.parametrize("error", [OSError("disk full"), KeyError("ref"), ValueError("bad")])
def test_any_runner_failure_fails_and_fences_the_job(env, error):
    queue, store, _, pr = env
    pr(1)

    def broken(job, attempt, stop):
        raise error

    job = audit_once(queue, store, 20, broken)
    assert job["state"] == "failed" and type(error).__name__ in job["error"]
    assert audits.get(store, job["attempt_id"])["state"] == "cancelled"


def test_an_import_that_loses_the_fence_race_keeps_the_supersession(env):
    queue, store, _, pr = env
    pr(1)

    def runner(job, attempt, stop):
        pr(2)  # superseded just as the agent exits; the import is then refused
        raise CirraError("audit attempt was cancelled or superseded; its report is refused")

    job = audit_once(queue, store, 20, runner)
    assert job["state"] == "superseded"


def test_unsupported_runner_outcome_fails_the_job(env):
    queue, store, _, pr = env
    pr(1)
    job = audit_once(queue, store, 20, lambda *args: {"state": "mystery"})
    assert job["state"] == "failed" and "unsupported outcome" in job["error"]


def test_requests_do_not_attach_to_work_being_cancelled(env):
    queue, store, _, pr = env
    first = pr(1)
    queue.next_job(20)
    queue.cancel(first["job_id"], "stop")  # running: cancellation is cooperative
    again = pr(1)
    assert not again["attached"] and again["job_id"] != first["job_id"]


def test_cancelling_after_import_lets_the_report_settle(env, tmp_path):
    queue, store, _, pr = env
    pr(1)
    waiting = audit_once(queue, store, 20, session)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE audits SET state='complete', completed_at=?, report_json=?, "
            "comparison_json=? WHERE attempt_id=?",
            (
                time.time(),
                json.dumps(build_report("pr", head(1), head(99))),
                json.dumps({"new": [], "open": [], "resolved": []}),
                waiting["attempt_id"],
            ),
        )
    with pytest.raises(CirraError, match="already imported"):
        queue.cancel(waiting["id"], "too late")
    audit_once(queue, store, 20, session)
    assert queue.read(waiting["id"])["state"] == "done"


def test_health_reports_unreadable_audit_records(tmp_path):
    from cirra.health import check

    AuditQueue(tmp_path)
    (tmp_path / "audit-queue" / "broken.json").write_text("{", encoding="utf-8")
    problems = check(tmp_path)["problems"]
    assert {
        "kind": "audit-queue",
        "detail": "unreadable audit records: ['broken.json']",
    } in problems


def test_old_finished_records_are_archived_and_capacity_is_bounded(env, monkeypatch):
    queue, store, config, pr = env
    first = pr(1)
    queue.cancel(first["job_id"], "stop")
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    path = queue.directory / (first["job_id"] + ".json")
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**record, "updated_at": old}), encoding="utf-8")
    pr(2)
    assert first["job_id"] not in {job["id"] for job in queue.jobs()}
    assert (queue.directory / "archive" / (first["job_id"] + ".json")).is_file()
    small = AuditQueue(queue.directory.parent, store, maximum=1)
    with pytest.raises(CirraError, match="capacity"):
        small.request(
            audits.audit_identity(config.audit, "full", audits.select_repository(config), head(3)),
            "repository",
            reason="schedule",
        )


def test_request_audit_validates_the_change_and_records_its_ref(tmp_path, policy_data, capsys):
    from cirra.cli import main

    config = audit_policy(tmp_path, policy_data)
    state = ["--state-dir", str(tmp_path / "cli")]
    base = ["request-audit", "--config", config.path, "--head", head(1), *state]
    pr = [*base, "--tier", "pr", "--base", head(2)]
    assert main([*pr, "--change", "-1"]) == 2
    assert "change number" in capsys.readouterr().err
    assert main([*pr, "--change", "7"]) == 0
    assert main([*base, "--tier", "full", "--ref", "main"]) == 0
    assert main([*base, "--tier", "release", "--ref", "../x"]) == 2
    capsys.readouterr()
    jobs = {job["tier"]: job for job in AuditQueue(tmp_path / "cli").jobs()}
    assert jobs["pr"]["context"]["ref"] == "refs/pull/7/head"
    assert jobs["full"]["context"]["ref"] == "refs/heads/main"
    assert jobs["full"]["scope"] == "repository"


def test_pr_gate_waits_for_the_baseline_then_compares_with_it(world, tmp_path):
    route, runner = world.make(agent(tmp_path, [("C011", "app.py", 11, 12, "critical")]))
    request(world, route, "app.py")  # no baseline: queues pr and a baseline full audit
    store = Store(world.state)
    queue = AuditQueue(world.state, store)
    github = FakeGitHub()
    publication = AuditPublication(
        world.state,
        {"main": route},
        lambda r: AuditCheckPublisher(r.repository, 5, "t", http=github),
    )
    waiting = audit_once(queue, store, 20, runner, on_complete=publication)
    assert waiting["tier"] == "pr" and waiting["state"] == "awaiting_baseline"
    assert github.posts == []
    store_baseline(world, route, [("C011", "app.py", 1, 2, "critical")])
    audit_once(
        queue, store, 20, lambda *args: {"state": "awaiting_import"}, on_complete=publication
    )
    settled = queue.read(waiting["id"])
    assert settled["state"] == "done"
    # The baseline already had C011, so it is not new and the check passes.
    assert settled["result"]["publication"]["conclusion"] == "success"
    assert len(github.posts) == 1


def test_pr_without_full_tier_publishes_without_a_baseline(world, tmp_path):
    route, runner = world.make(agent(tmp_path))
    route.policy = replace(route.policy, audit=replace(route.policy.audit, tiers=("pr",)))
    request(world, route, "app.py")
    store = Store(world.state)
    github = FakeGitHub()
    publication = AuditPublication(
        world.state,
        {"main": route},
        lambda r: AuditCheckPublisher(r.repository, 5, "t", http=github),
    )
    job = audit_once(AuditQueue(world.state, store), store, 20, runner, on_complete=publication)
    assert job["state"] == "done" and len(github.posts) == 1


def test_changed_policy_refuses_to_run_or_publish_old_requests(world, tmp_path):
    route, runner = world.make(agent(tmp_path))
    store_baseline(world, route)
    request(world, route, "app.py")
    store = Store(world.state)
    queue = AuditQueue(world.state, store)
    original = route.policy
    route.policy = replace(
        original, audit=replace(original.audit, gates={**original.audit.gates, "pr": "none"})
    )
    job = audit_once(queue, store, 20, runner)
    assert job["state"] == "failed" and "policy changed" in job["error"]

    route.policy = original
    publication = AuditPublication(world.state, {"main": route}, lambda r: None)
    request(world, route, "app.py")  # a fresh attempt under the original policy
    done = audit_once(queue, store, 20, runner)
    assert done["state"] == "done"
    route.policy = replace(
        original, audit=replace(original.audit, gates={**original.audit.gates, "pr": "none"})
    )
    receipt = publication(done, audits.get(store, done["attempt_id"]))
    assert receipt["state"] == "failed" and "policy changed" in receipt["reason"]


def test_long_audits_keep_the_worker_heartbeat_current(world, tmp_path):
    route, runner = world.make(agent(tmp_path, source="import time\ntime.sleep(0.6)\n"))
    beats = []
    runner.heartbeat = lambda: beats.append(time.monotonic())
    store_baseline(world, route)
    request(world, route, "app.py")
    store = Store(world.state)
    audit_once(AuditQueue(world.state, store), store, 20, runner)
    assert len(beats) >= 3


def test_agent_output_is_bounded(world, tmp_path, monkeypatch):
    monkeypatch.setattr(audit_worker, "MAX_AGENT_LOG_BYTES", 1000)
    noisy = (
        "import time\n"
        "for _ in range(200):\n"
        "    print('x' * 100, flush=True)\n"
        "    time.sleep(0.01)\n"
    )
    route, runner = world.make(agent(tmp_path, source=noisy))
    store_baseline(world, route)
    request(world, route, "app.py")
    store = Store(world.state)
    job = audit_once(AuditQueue(world.state, store), store, 20, runner)
    assert job["state"] == "failed" and "output exceeded" in job["error"]


def test_awaiting_baseline_is_active_work():
    assert "awaiting_baseline" in job_queue.ACTIVE_AUDIT


# --- Review findings on #20 ------------------------------------------------


def full_job(queue, store, config, revision=1):
    identity = audits.audit_identity(
        config.audit, "full", audits.select_repository(config), head(revision)
    )
    return queue.request(identity, "repository", reason="schedule")


def slugs():
    return [domain["slug"] for domain in audits.catalog_domains()]


def test_checkpoints_carry_findings_and_reject_malformed_rows(tmp_path):
    finding = {
        "path": "app.py",
        "side": "new",
        "start_line": 1,
        "end_line": 2,
        "severity": "high",
        "title": "[C011] Missing timeout",
        "evidence": "No timeout.",
    }
    row = {"id": "C011", "status": "missing", "evidence": []}
    good = {"domain": "reliability-and-resilience", "coverage": [row], "findings": [finding]}
    path = tmp_path / "reliability-and-resilience.json"
    path.write_text(json.dumps(good), encoding="utf-8")
    assert audits.valid_checkpoint(path)["findings"] == [finding]
    for bad in (
        {**good, "coverage": [{**row, "evidence": 1}]},
        {**good, "coverage": [{**row, "evidence": [{"path": "a.py", "start_line": "1"}]}]},
        {**good, "coverage": [{**row, "status": "sort of"}]},
        {**good, "findings": [{**finding, "title": "[C058] Another domain"}]},
        {**good, "findings": [{**finding, "extra": True}]},
        {**good, "findings": "none"},
    ):
        path.write_text(json.dumps(bad), encoding="utf-8")
        assert audits.valid_checkpoint(path) is None


def test_no_preemption_once_every_domain_is_checkpointed(env):
    queue, store, config, pr = env
    full_job(queue, store, config)
    seen = []

    def runner(job, attempt, stop):
        pr(5)  # priority work becomes runnable during the last domain
        seen.append(stop(checkpoints=slugs()))
        return {"state": "awaiting_import"}

    audit_once(queue, store, 20, runner)
    assert seen == [None]


def test_no_preemption_without_admission_for_the_waiting_work(env):
    queue, store, config, pr = env
    full_job(queue, store, config)
    seen = []

    def runner(job, attempt, stop):
        pr(5)
        seen.append(stop(checkpoints=slugs()[:1]))
        return {"state": "awaiting_import"}

    audit_once(queue, store, 1, runner)  # the full audit used the only slot
    assert seen == [None]


def test_resuming_a_preempted_audit_the_same_day_is_free(env):
    queue, store, config, pr = env
    full = full_job(queue, store, config)

    def runner(job, attempt, stop):
        if job["tier"] == "full" and not job.get("preemptions"):
            pr(5)
            return {"state": "stopped", "reason": stop(checkpoints=slugs()[:1])}
        return {"state": "awaiting_import"}

    assert audit_once(queue, store, 2, runner)["state"] == "queued"  # preempted
    assert audit_once(queue, store, 2, runner)["tier"] == "pr"  # second slot
    resumed = audit_once(queue, store, 2, runner)  # no slot left, but already admitted
    assert resumed["id"] == full["job_id"] and resumed["state"] == "awaiting_import"


def test_cancelling_a_preempted_job_fences_its_attempt(env):
    queue, store, config, pr = env
    full_job(queue, store, config)

    def runner(job, attempt, stop):
        pr(5)
        return {"state": "stopped", "reason": stop(checkpoints=slugs()[:1])}

    preempted = audit_once(queue, store, 20, runner)
    assert preempted["state"] == "queued" and preempted["attempt_id"]
    queue.cancel(preempted["id"], "no longer needed")
    assert audits.get(store, preempted["attempt_id"])["state"] == "cancelled"


def test_scheduled_reaudit_of_an_unchanged_head_opens_a_new_attempt(world):
    route, _ = world.make()
    store = Store(world.state)
    repository = audits.select_repository(route.policy)
    identity = audits.audit_identity(route.policy.audit, "full", repository, world.base)
    old, _ = audits.begin(store, identity)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE audits SET state='complete', completed_at=?, report_json=? WHERE attempt_id=?",
            (
                time.time() - 8 * 86400,
                json.dumps(build_report("full", world.base)),
                old["attempt_id"],
            ),
        )
    from cirra.audit_worker import request_audits

    [receipt] = request_audits(
        world.state, route, {"kind": "schedule"}, default_head=lambda: ("main", world.base)
    )
    assert receipt["reason"] == "schedule" and not receipt["attached"]
    queue = AuditQueue(world.state, store)
    job = audit_once(queue, store, 20, session)
    new = audits.get(store, job["attempt_id"])
    assert new["audit_id"] == old["audit_id"] and new["number"] == 2
    again = request_audits(
        world.state, route, {"kind": "schedule"}, default_head=lambda: ("main", world.base)
    )
    assert again[0]["attached"]  # the running re-audit absorbs further ticks


def test_checkpoints_are_not_reused_across_catalogs(world, tmp_path):
    route, runner = world.make()
    store = Store(world.state)
    repository = audits.select_repository(route.policy)
    identity = audits.audit_identity(route.policy.audit, "full", repository, world.base)
    old, _ = audits.begin(store, {**identity, "catalog_digest": "0" * 64})
    source = runner.attempt_dir(old["attempt_id"]) / "work" / "checkpoints"
    source.mkdir(parents=True)
    (source / "security.json").write_text(
        json.dumps({"domain": "security", "coverage": []}), encoding="utf-8"
    )
    target = tmp_path / "target"
    target.mkdir()
    job = {"resume_from": old["attempt_id"], "identity": identity}
    assert runner.resume(store, job, world.origin.path, target) == []


def test_evidence_free_domains_are_rerun_after_a_new_head(world, tmp_path):
    route, runner = world.make()
    store = Store(world.state)
    repository = audits.select_repository(route.policy)
    old, _ = audits.begin(
        store, audits.audit_identity(route.policy.audit, "full", repository, world.base)
    )
    source = runner.attempt_dir(old["attempt_id"]) / "work" / "checkpoints"
    source.mkdir(parents=True)
    cited = {
        "id": "C064",
        "status": "implemented",
        "evidence": [{"path": "pyproject.toml", "start_line": 1, "end_line": 1}],
    }
    absent = {"id": "C065", "status": "not_applicable", "evidence": []}
    (source / "security.json").write_text(
        json.dumps({"domain": "security", "coverage": [{**cited, "id": "X01"}]}), encoding="utf-8"
    )
    (source / "observability-and-operations.json").write_text(
        json.dumps({"domain": "observability-and-operations", "coverage": [cited, absent]}),
        encoding="utf-8",
    )
    from cirra.audit_checkout import prepare_checkout

    checkout = prepare_checkout(
        world.state,
        "github",
        "example/repo",
        "refs/pull/7/head",
        world.head,
        tmp_path / "co",
        url=str(world.origin.path),
        protocols=("file",),
    )
    job = {
        "resume_from": old["attempt_id"],
        "identity": audits.audit_identity(route.policy.audit, "full", repository, world.head),
    }
    target = tmp_path / "target"
    target.mkdir()
    assert runner.resume(store, job, checkout, target) == ["security"]


def test_same_key_replacement_is_allowed_at_capacity(env):
    queue, store, config, _ = env
    small = AuditQueue(queue.directory.parent, store, maximum=1)
    repository = audits.select_repository(config)

    def pr(revision):
        identity = audits.audit_identity(config.audit, "pr", repository, head(revision), head(99))
        return small.request(identity, "change:1", reason="change")

    first = pr(1)
    second = pr(2)  # replaces the queued request: no net growth
    assert small.read(first["job_id"])["superseded_by"] == second["job_id"]


def test_records_missing_required_fields_are_reported_not_loaded(tmp_path):
    from cirra.health import check

    queue = AuditQueue(tmp_path)
    (queue.directory / "partial.json").write_text('{"sequence": 1}', encoding="utf-8")
    jobs, unreadable = queue.scan()
    assert jobs == [] and unreadable == ["partial.json"]
    assert any(problem["kind"] == "audit-queue" for problem in check(tmp_path)["problems"])


def test_records_with_a_mistyped_sequence_are_reported(tmp_path):
    queue = AuditQueue(tmp_path)
    record = {"sequence": "1", "id": "a" * 32, "state": "queued", "identity": {}}
    (queue.directory / ("a" * 32 + ".json")).write_text(json.dumps(record), encoding="utf-8")
    assert queue.scan() == ([], ["a" * 32 + ".json"])


def test_log_cap_also_applies_after_a_fast_exit(world, tmp_path, monkeypatch):
    monkeypatch.setattr(audit_worker, "MAX_AGENT_LOG_BYTES", 1000)
    route, runner = world.make(agent(tmp_path, source="print('x' * 5000)\n"))
    runner.poll_seconds = 3  # the agent exits before the first size check
    store_baseline(world, route)
    request(world, route, "app.py")
    store = Store(world.state)
    job = audit_once(AuditQueue(world.state, store), store, 20, runner)
    assert job["state"] == "failed" and "output exceeded" in job["error"]


def test_a_renamed_cited_file_invalidates_its_checkpoint(world, tmp_path):
    from cirra.audit_checkout import prepare_checkout

    world.origin.git("switch", "feature")
    world.origin.git("mv", "pyproject.toml", "renamed.toml")
    world.origin.git("commit", "-m", "rename")
    renamed = world.origin.git("rev-parse", "HEAD")
    world.origin.git("update-ref", "refs/pull/8/head", renamed)
    route, runner = world.make()
    store = Store(world.state)
    repository = audits.select_repository(route.policy)
    old, _ = audits.begin(
        store, audits.audit_identity(route.policy.audit, "full", repository, world.head)
    )
    source = runner.attempt_dir(old["attempt_id"]) / "work" / "checkpoints"
    source.mkdir(parents=True)
    row = {
        "id": "X01",
        "status": "implemented",
        "evidence": [{"path": "pyproject.toml", "start_line": 1, "end_line": 1}],
    }
    (source / "security.json").write_text(
        json.dumps({"domain": "security", "coverage": [row]}), encoding="utf-8"
    )
    checkout = prepare_checkout(
        world.state,
        "github",
        "example/repo",
        "refs/pull/8/head",
        renamed,
        tmp_path / "co",
        url=str(world.origin.path),
        protocols=("file",),
    )
    job = {
        "resume_from": old["attempt_id"],
        "identity": audits.audit_identity(route.policy.audit, "full", repository, renamed),
    }
    target = tmp_path / "target"
    target.mkdir()
    assert runner.resume(store, job, checkout, target) == []


def test_first_worker_pass_runs_the_schedule_check(world, monkeypatch):
    from cirra import worker

    route, _ = world.make()
    store = Store(world.state)
    repository = audits.select_repository(route.policy)
    old, _ = audits.begin(
        store, audits.audit_identity(route.policy.audit, "full", repository, world.base)
    )
    with store.transaction() as connection:
        connection.execute(
            "UPDATE audits SET state='complete', completed_at=?, report_json=? WHERE attempt_id=?",
            (
                time.time() - 8 * 86400,
                json.dumps(build_report("full", world.base)),
                old["attempt_id"],
            ),
        )
    monkeypatch.setattr(worker, "default_branch_head", lambda route, reader: ("main", world.base))
    processor = worker.Processor(world.state, lambda name: None, None)
    [receipt] = processor.schedule_audits({"main": route}, now=0)
    assert receipt["reason"] == "schedule" and not receipt["attached"]


def test_attention_jobs_are_archived_too(env):
    queue, store, _, pr = env
    first = pr(1)
    path = queue.directory / (first["job_id"] + ".json")
    record = json.loads(path.read_text(encoding="utf-8"))
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    path.write_text(
        json.dumps({**record, "state": "attention", "updated_at": old}), encoding="utf-8"
    )
    pr(2)
    assert (queue.directory / "archive" / (first["job_id"] + ".json")).is_file()
