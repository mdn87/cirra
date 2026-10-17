"""Regression tests for the review findings on the audit queue and worker (#17, #18)."""

import json
import os
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from audit_fixtures import audit_policy, build_report, domain_rows
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
    row = {"id": "C011", "status": "missing", "priority": "high", "evidence": []}
    others = [r for r in domain_rows("reliability-and-resilience") if r["id"] != "C011"]
    good = {
        "domain": "reliability-and-resilience",
        "coverage": [row, *others],
        "findings": [finding],
    }
    path = tmp_path / "reliability-and-resilience.json"
    path.write_text(json.dumps(good), encoding="utf-8")
    assert audits.valid_checkpoint(path)["findings"] == [finding]
    for bad in (
        {**good, "coverage": [{**row, "evidence": 1}, *others]},
        {**good, "coverage": [{**row, "evidence": [{"path": "a.py", "start_line": "1"}]}, *others]},
        {**good, "coverage": [{**row, "status": "sort of"}, *others]},
        {**good, "coverage": others},  # a concept is missing
        {**good, "coverage": [row, row, *others[1:]]},  # a duplicate instead of a concept
        {**good, "findings": [{**finding, "severity": 1}]},
        {**good, "findings": [{**finding, "evidence": " "}]},
        {**good, "findings": [{**finding, "side": "old"}]},
        {**good, "findings": [{**finding, "title": "[C011]"}]},
        {**good, "findings": [{**finding, "severity": "low"}]},  # not the row priority
        {**good, "findings": []},  # the missing row has no finding
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
        json.dumps({"domain": "security", "coverage": domain_rows("security", ["app.py"])}),
        encoding="utf-8",
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
    absent = {"id": "C065", "status": "not_applicable", "evidence": []}
    (source / "security.json").write_text(
        json.dumps({"domain": "security", "coverage": domain_rows("security", ["pyproject.toml"])}),
        encoding="utf-8",
    )
    (source / "observability-and-operations.json").write_text(
        json.dumps(
            {
                "domain": "observability-and-operations",
                "coverage": [
                    *domain_rows("observability-and-operations", ["pyproject.toml"])[1:],
                    {**absent, "id": domain_rows("observability-and-operations")[0]["id"]},
                ],
            }
        ),
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
    assert runner.resume(store, job, checkout, target) == []  # any change reruns all


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
    (source / "security.json").write_text(
        json.dumps({"domain": "security", "coverage": domain_rows("security", ["pyproject.toml"])}),
        encoding="utf-8",
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


# --- Third Codex round on #20 -----------------------------------------------


def test_a_changed_finding_path_invalidates_its_checkpoint(world, tmp_path):
    from cirra.audit_checkout import prepare_checkout

    route, runner = world.make()
    store = Store(world.state)
    repository = audits.select_repository(route.policy)
    old, _ = audits.begin(
        store, audits.audit_identity(route.policy.audit, "full", repository, world.base)
    )
    source = runner.attempt_dir(old["attempt_id"]) / "work" / "checkpoints"
    source.mkdir(parents=True)
    rows = domain_rows("security", ["pyproject.toml"])
    finding = {
        "path": "app.py",  # changed between the heads, unlike the cited evidence
        "side": "new",
        "start_line": 1,
        "end_line": 1,
        "severity": "high",
        "title": f"[{rows[0]['id']}] Fixture",
        "evidence": "Fixture.",
    }
    (source / "security.json").write_text(
        json.dumps({"domain": "security", "coverage": rows, "findings": [finding]}),
        encoding="utf-8",
    )
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
    assert runner.resume(store, job, checkout, target) == []


@pytest.mark.skipif(os.name == "nt", reason="agents inherit the lock only on POSIX")
def test_a_surviving_agent_is_killed_before_relaunch(world, tmp_path):
    _, runner = world.make()
    work = tmp_path / "work"
    work.mkdir()
    with (work / "first.log").open("ab") as log:
        survivor = runner.launch(
            [sys.executable, "-c", "import time; time.sleep(60)"], work, None, log
        )
    # The worker "crashed": only the survivor still holds the lock.
    with (work / "second.log").open("ab") as log:
        second = runner.launch([sys.executable, "-c", "pass"], work, None, log)
    assert survivor.wait(10) is not None
    assert second.wait(10) == 0


def test_a_stop_during_baseline_wait_prevents_publication(world, tmp_path):
    route, runner = world.make(agent(tmp_path))
    request(world, route, "app.py")
    store = Store(world.state)
    queue = AuditQueue(world.state, store)
    github = FakeGitHub()
    publication = AuditPublication(
        world.state,
        {"main": route},
        lambda r: AuditCheckPublisher(r.repository, 5, "t", http=github),
    )
    waiting = audit_once(queue, store, 20, runner, on_complete=publication)
    assert waiting["state"] == "awaiting_baseline"
    store_baseline(world, route)
    queue.update(waiting["id"], superseded_by="f" * 32)  # lands while it waits
    audit_once(
        queue, store, 20, lambda *args: {"state": "awaiting_import"}, on_complete=publication
    )
    assert queue.read(waiting["id"])["state"] == "superseded"
    assert github.posts == []


def test_a_baseline_that_prs_wait_on_runs_before_more_pr_work(env):
    queue, store, config, pr = env
    repository = audits.select_repository(config)
    baseline = queue.request(
        audits.audit_identity(config.audit, "full", repository, head(50)),
        "repository",
        reason="baseline",
    )
    waiting = pr(1)
    queue.update(waiting["job_id"], state="awaiting_baseline")
    pr(2, change=2)
    assert queue.runnable()[0]["id"] == baseline["job_id"]


def test_operator_requests_pick_the_route_with_their_policy(world, policy_data, tmp_path):
    from types import SimpleNamespace

    from cirra.audit_worker import route_for

    route, _ = world.make()
    strict = audit_policy(
        tmp_path, policy_data, {"tiers": ["pr", "full"], "gates": {"pr": "low"}}, "strict.yaml"
    )
    other = SimpleNamespace(**{**vars(route), "name": "strict", "policy": strict})
    identity = audits.audit_identity(
        strict.audit, "full", audits.select_repository(strict), world.head
    )
    assert route_for({"main": route, "strict": other}, None, identity) is other


def test_publication_rechecks_for_a_stop_right_before_writing(world, tmp_path):
    route, runner = world.make(agent(tmp_path))
    store_baseline(world, route)
    request(world, route, "app.py")
    store = Store(world.state)
    queue = AuditQueue(world.state, store)
    job = audit_once(queue, store, 20, runner)  # completes without publishing
    github = FakeGitHub()
    publication = AuditPublication(
        world.state,
        {"main": route},
        lambda r: AuditCheckPublisher(r.repository, 5, "t", http=github),
    )
    queue.update(job["id"], cancel_requested=True)  # arrives after settle's own check
    receipt = publication(job, audits.get(store, job["attempt_id"]))
    assert receipt == {"state": "stopped", "reason": "cancelled"} and github.posts == []


# --- Fifth Codex round on #20 ------------------------------------------------


def test_records_missing_queue_fields_are_reported(env):
    queue, store, _, pr = env
    job = pr(1)
    path = queue.directory / (job["job_id"] + ".json")
    record = json.loads(path.read_text(encoding="utf-8"))
    del record["not_before"]
    path.write_text(json.dumps(record), encoding="utf-8")
    assert queue.scan() == ([], [job["job_id"] + ".json"])
    assert audit_once(queue, store, 20, lambda *args: {"state": "awaiting_import"}) is None


def test_a_blocking_baseline_outranks_release_work(env):
    queue, store, config, pr = env
    repository = audits.select_repository(config)
    queue.request(
        audits.audit_identity(config.audit, "release", repository, head(60)),
        "release:main",
        reason="release",
    )
    baseline = queue.request(
        audits.audit_identity(config.audit, "full", repository, head(50)),
        "repository",
        reason="baseline",
    )
    waiting = pr(1)
    queue.update(waiting["job_id"], state="awaiting_baseline")
    assert queue.runnable()[0]["id"] == baseline["job_id"]


def test_a_running_same_key_job_is_replaceable_at_capacity(env):
    queue, store, config, _ = env
    small = AuditQueue(queue.directory.parent, store, maximum=1)
    repository = audits.select_repository(config)

    def pr(revision):
        identity = audits.audit_identity(config.audit, "pr", repository, head(revision), head(99))
        return small.request(identity, "change:1", reason="change")

    first = pr(1)
    small.next_job(20)  # now running
    second = pr(2)
    assert small.read(first["job_id"])["superseded_by"] == second["job_id"]


# --- Sixth Codex round on #20 ------------------------------------------------


def test_a_changed_non_ascii_path_invalidates_its_checkpoint(world, tmp_path):
    from cirra.audit_checkout import prepare_checkout

    world.origin.git("switch", "main")
    world.origin.commit({"café.py": "one\n"})
    before = world.origin.git("rev-parse", "HEAD")
    after = world.origin.commit({"café.py": "two\n"})
    world.origin.git("update-ref", "refs/pull/9/head", after)
    route, runner = world.make()
    store = Store(world.state)
    repository = audits.select_repository(route.policy)
    old, _ = audits.begin(
        store, audits.audit_identity(route.policy.audit, "full", repository, before)
    )
    source = runner.attempt_dir(old["attempt_id"]) / "work" / "checkpoints"
    source.mkdir(parents=True)
    (source / "security.json").write_text(
        json.dumps({"domain": "security", "coverage": domain_rows("security", ["café.py"])}),
        encoding="utf-8",
    )
    checkout = prepare_checkout(
        world.state,
        "github",
        "example/repo",
        "refs/pull/9/head",
        after,
        tmp_path / "co",
        url=str(world.origin.path),
        protocols=("file",),
    )
    job = {
        "resume_from": old["attempt_id"],
        "identity": audits.audit_identity(route.policy.audit, "full", repository, after),
    }
    target = tmp_path / "target"
    target.mkdir()
    assert runner.resume(store, job, checkout, target) == []


def test_a_baseline_from_another_catalog_makes_the_pr_wait(world, tmp_path):
    route, runner = world.make(agent(tmp_path))
    store = Store(world.state)
    repository = audits.select_repository(route.policy)
    identity = audits.audit_identity(route.policy.audit, "full", repository, world.base)
    old, _ = audits.begin(store, {**identity, "catalog_digest": "0" * 64})
    with store.transaction() as connection:
        connection.execute(
            "UPDATE audits SET state='complete', completed_at=?, report_json=? WHERE attempt_id=?",
            (time.time(), json.dumps(build_report("full", world.base)), old["attempt_id"]),
        )
    request(world, route, "app.py")
    github = FakeGitHub()
    publication = AuditPublication(
        world.state,
        {"main": route},
        lambda r: AuditCheckPublisher(r.repository, 5, "t", http=github),
    )
    queue = AuditQueue(world.state, store)
    job = audit_once(queue, store, 20, runner, on_complete=publication)
    assert job["tier"] == "pr" and job["state"] == "awaiting_baseline", job
    assert github.posts == []


def test_oversized_checkpoints_are_rejected_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(audits, "MAX_CHECKPOINT_BYTES", 100)
    path = tmp_path / "security.json"
    path.write_text(
        json.dumps({"domain": "security", "coverage": domain_rows("security")}), encoding="utf-8"
    )
    assert audits.valid_checkpoint(path) is None


# --- Seventh Codex round on #20 ----------------------------------------------


def test_a_pending_review_delivery_preempts_a_full_audit(env):
    queue, store, config, _ = env
    full_job(queue, store, config)
    seen = []

    def runner(job, attempt, stop):
        seen.append(stop(checkpoints=slugs()[:1]))
        return (
            {"state": "stopped", "reason": seen[-1]} if seen[-1] else {"state": "awaiting_import"}
        )

    job = audit_once(queue, store, 20, runner, interrupt=lambda: True)
    assert seen == ["preempted"] and job["state"] == "queued"


def test_git_listing_keeps_leading_whitespace(tmp_path):
    from audit_fixtures import Repository

    repo = Repository(tmp_path / "r")
    first = repo.commit({"a.py": "a\n"})
    second = repo.commit({" spaced.py": "b\n"})
    listing = audits.git(repo.path, "diff", "--name-only", "-z", first, second, strip=False)
    assert listing.split("\0")[0] == " spaced.py"


# --- Eighth Codex round on #20 -----------------------------------------------


def test_a_skill_only_upgrade_queues_a_replacement_baseline(world):
    from test_audit_full import schedule

    route, _ = world.make()
    store = Store(world.state)
    repository = audits.select_repository(route.policy)
    identity = audits.audit_identity(route.policy.audit, "full", repository, world.base)
    old, _ = audits.begin(store, {**identity, "skill_version": "0.9.0"})
    with store.transaction() as connection:
        connection.execute(
            "UPDATE audits SET state='complete', completed_at=?, report_json=? WHERE attempt_id=?",
            (time.time(), json.dumps(build_report("full", world.base)), old["attempt_id"]),
        )
    assert schedule(world, route) == [("full", "catalog")]
    [job] = AuditQueue(world.state).jobs()
    assert job["identity"]["skill_version"] == audits.vendored_identity()["skill_version"]


def blocking_baseline(queue, store, config, pr):
    """A repository baseline that a finished pr audit is waiting on."""
    baseline = full_job(queue, store, config, revision=50)
    waiting = queue.read(pr(1)["job_id"])
    attempt, _ = audits.begin(store, waiting["identity"], waiting["scope"])
    queue.update(waiting["id"], state="awaiting_baseline", attempt_id=attempt["attempt_id"])
    return baseline


@pytest.mark.parametrize("interrupt", [None, lambda: True])
def test_a_baseline_that_prs_wait_on_is_not_preempted_by_work_it_outranks(env, interrupt):
    queue, store, config, pr = env
    baseline = blocking_baseline(queue, store, config, pr)
    seen = []

    def runner(job, attempt, stop):
        pr(2, change=2)  # more pr work arrives while the baseline runs
        seen.append(stop(checkpoints=slugs()[:1]))
        return {"state": "awaiting_import"}

    job = audit_once(queue, store, 20, runner, interrupt=interrupt)
    assert job["id"] == baseline["job_id"]
    assert seen == [None]


def test_an_ordinary_full_audit_is_still_preempted_by_pr_work(env):
    queue, store, config, pr = env
    full_job(queue, store, config)
    seen = []

    def runner(job, attempt, stop):
        pr(2, change=2)
        seen.append(stop(checkpoints=slugs()[:1]))
        return {"state": "stopped", "reason": seen[-1]}

    assert audit_once(queue, store, 20, runner)["state"] == "queued"
    assert seen == ["preempted"]


def test_a_full_audit_is_not_preempted_by_work_of_the_same_rank(env):
    queue, store, config, _ = env
    full_job(queue, store, config)
    seen = []

    def runner(job, attempt, stop):
        structural = audits.audit_identity(
            config.audit, "full", audits.select_repository(config), head(8)
        )
        queue.request(structural, "change:3", reason="structural")
        seen.append(stop(checkpoints=slugs()[:1]))
        return {"state": "awaiting_import"}

    audit_once(queue, store, 20, runner)
    assert seen == [None]


def test_effective_priority_ranks_a_blocking_baseline_first(env):
    queue, store, config, pr = env
    baseline = blocking_baseline(queue, store, config, pr)
    jobs = {job["id"]: job for job in queue.jobs()}
    assert queue.priority(jobs[baseline["job_id"]]) == -1
    other = queue.read(pr(2, change=2)["job_id"])
    assert queue.priority(other) == job_queue.AUDIT_PRIORITY["pr"]


class FakeProvider:
    """A provider client whose repository reports `native` as its ID."""

    native = 5

    def __init__(self, repository, token=None):
        self.repository = repository

    def get(self, route=""):
        if route == "":
            return {"id": self.native, "default_branch": "main"}, {}
        return {"commit": {"sha": head(7), "id": head(7)}}, {}


@pytest.mark.parametrize(
    ("provider", "client"),
    [("github", "cirra.github.GitHubClient"), ("gitlab", "cirra.gitlab.GitLabClient")],
)
def test_default_branch_head_verifies_the_repository_identity(monkeypatch, provider, client):
    from types import SimpleNamespace

    monkeypatch.setattr(client, FakeProvider)
    route = SimpleNamespace(provider=provider, repository="acme/app", repository_id="5")
    assert audit_worker.default_branch_head(route, None) == ("main", head(7))
    monkeypatch.setattr(FakeProvider, "native", 6)  # the path now names another repository
    with pytest.raises(CirraError, match="identity changed"):
        audit_worker.default_branch_head(route, None)
