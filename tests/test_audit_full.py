"""Stage 6 of the production-audit tiers plan: schedules, checkpoints, preemption."""

import json
import sys
import time

from audit_fixtures import build_report, domain_rows
from test_audit_worker import TESTS, store_baseline

from cirra import audits
from cirra.audit_checkout import prepare_checkout
from cirra.audit_worker import audit_once, request_audits
from cirra.job_queue import AuditQueue
from cirra.store import Store

# Writes one checkpoint per domain, logging each domain it works on. On a fresh
# run it pauses after each domain so a preemption can land between domains.
FULL_AGENT = """
import json, os, sys, time
from pathlib import Path
# The agent gets no PYTHONPATH from Cirra; find cirra and the fixtures directly.
sys.path[:0] = [{tests!r}, {src!r}]
from audit_fixtures import build_report, write_report
work = Path(os.environ["CIRRA_AUDIT_DIR"])
packet = json.loads(Path(os.environ["CIRRA_AUDIT_PACKET"]).read_text(encoding="utf-8"))
head, base = packet["repository"]["head"], packet["repository"]["base"]
if packet["tier"] == "full":
    checkpoints = work / "checkpoints"
    done = {{path.stem for path in checkpoints.glob("*.json")}}
    fresh = not done
    for domain in packet["domains"]:
        if domain["slug"] in done:
            continue
        if (work / "stop").exists():
            sys.exit(0)
        rows = [
            {{"id": cid, "concept": cid, "status": "not_applicable", "priority": None,
              "likelihood": None, "effort": None, "rationale": "fixture", "evidence": []}}
            for cid in domain["ids"]
        ]
        (checkpoints / (domain["slug"] + ".json")).write_text(
            json.dumps({{"domain": domain["slug"], "coverage": rows}}), encoding="utf-8"
        )
        with open({log!r}, "a", encoding="utf-8") as log:
            log.write(domain["slug"] + "\\n")
        for _ in range(12 if fresh else 0):
            if (work / "stop").exists():
                sys.exit(0)
            time.sleep(0.05)
report = build_report(packet["tier"], head, base, repo_path=os.environ["CIRRA_AUDIT_CHECKOUT"])
write_report(work, report)
"""


def full_agent(tmp_path):
    log = tmp_path / "domains.log"
    script = tmp_path / "full-agent.py"
    source = FULL_AGENT.format(tests=str(TESTS), src=str(TESTS.parent / "src"), log=str(log))
    script.write_text(source, encoding="utf-8")
    return {"kind": "command", "command": [sys.executable, str(script)]}, log


def test_preempted_full_audit_resumes_without_rerunning_completed_domains(world, tmp_path):
    runner_policy, log = full_agent(tmp_path)
    route, runner = world.make(runner_policy)
    store = Store(world.state)
    queue = AuditQueue(world.state, store)
    repository = audits.select_repository(route.policy)
    full = queue.request(
        audits.audit_identity(route.policy.audit, "full", repository, world.base),
        "repository",
        reason="baseline",
        context={"route": "main", "change": None, "ref": "refs/heads/main"},
    )
    # The pull request arrives while the full audit is underway.
    queue.request(
        audits.audit_identity(route.policy.audit, "pr", repository, world.head, world.base),
        "change:7",
        reason="change",
        debounce_seconds=1.0,
        context={"route": "main", "change": 7, "ref": "refs/pull/7/head"},
    )
    first = audit_once(queue, store, 20, runner)
    assert first["id"] == full["job_id"]
    assert first["state"] == "queued" and first["preemptions"] == 1, first
    done_before = log.read_text(encoding="utf-8").split()
    assert 1 <= len(done_before) < 10
    assert first["checkpoints"] == sorted(done_before)

    second = audit_once(queue, store, 20, runner)
    assert second["tier"] == "pr" and second["state"] == "done", second

    third = audit_once(queue, store, 20, runner)
    assert third["id"] == full["job_id"] and third["state"] == "done", third
    assert third["attempt_id"] == first["attempt_id"]
    worked = log.read_text(encoding="utf-8").split()
    slugs = [domain["slug"] for domain in audits.catalog_domains()]
    assert sorted(worked) == sorted(slugs)  # every domain exactly once
    newest = audits.latest_full(store, audits.repository_key(repository))
    assert newest["attempt_id"] == first["attempt_id"]


def checkpoint(directory, slug, evidence_paths):
    rows = domain_rows(slug, evidence_paths)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{slug}.json").write_text(
        json.dumps({"domain": slug, "coverage": rows}), encoding="utf-8"
    )


def test_superseded_full_audit_reuses_checkpoints_only_when_no_file_changed(world, tmp_path):
    world.origin.git("switch", "feature")
    world.origin.git("commit", "--allow-empty", "-m", "no file changes")
    same_tree = world.origin.git("rev-parse", "HEAD")
    world.origin.git("update-ref", "refs/pull/8/head", same_tree)
    route, runner = world.make()
    store = Store(world.state)
    repository = audits.select_repository(route.policy)
    old, _ = audits.begin(
        store, audits.audit_identity(route.policy.audit, "full", repository, world.base)
    )
    source = runner.attempt_dir(old["attempt_id"]) / "work" / "checkpoints"
    checkpoint(source, "reliability-and-resilience", ["app.py"])
    checkpoint(source, "security", ["pyproject.toml"])
    checkpoint(source, "traffic-caching-and-edge", [])
    (source / "security-copy.json").write_text("{}", encoding="utf-8")  # not a domain
    checkout = prepare_checkout(
        world.state,
        "github",
        "example/repo",
        "refs/pull/7/head",
        world.head,
        tmp_path / "new-checkout",
        url=str(world.origin.path),
        protocols=("file",),
    )
    job = {
        "resume_from": old["attempt_id"],
        "identity": audits.audit_identity(route.policy.audit, "full", repository, world.head),
    }
    target = tmp_path / "target"
    target.mkdir()
    # app.py changed between the heads: every domain reruns, cited or not.
    assert runner.resume(store, job, checkout, target) == []

    unchanged = prepare_checkout(
        world.state,
        "github",
        "example/repo",
        "refs/pull/8/head",
        same_tree,
        tmp_path / "same-tree",
        url=str(world.origin.path),
        protocols=("file",),
    )
    old_head, _ = audits.begin(
        store, audits.audit_identity(route.policy.audit, "full", repository, world.head)
    )
    moved = runner.attempt_dir(old_head["attempt_id"]) / "work" / "checkpoints"
    moved.mkdir(parents=True)
    for path in source.glob("*.json"):
        (moved / path.name).write_bytes(path.read_bytes())
    same = {
        "resume_from": old_head["attempt_id"],
        "identity": audits.audit_identity(route.policy.audit, "full", repository, same_tree),
    }
    copied = runner.resume(store, same, unchanged, target)
    assert copied == ["reliability-and-resilience", "security", "traffic-caching-and-edge"]
    assert sorted(path.stem for path in target.glob("*.json")) == copied
    # An attempt that already has checkpoints keeps its own progress.
    assert runner.resume(store, same, unchanged, target) == []


def test_supersession_links_the_new_full_request_to_the_old_attempt(world):
    route, _ = world.make()
    store = Store(world.state)
    queue = AuditQueue(world.state, store)
    repository = audits.select_repository(route.policy)
    first = queue.request(
        audits.audit_identity(route.policy.audit, "full", repository, world.base),
        "repository",
        reason="schedule",
    )
    claimed = queue.next_job(20)
    attempt, _ = audits.begin(store, claimed["identity"], claimed["scope"])
    queue.update(claimed["id"], attempt_id=attempt["attempt_id"])
    second = queue.request(
        audits.audit_identity(route.policy.audit, "full", repository, world.head),
        "repository",
        reason="schedule",
    )
    assert queue.read(first["job_id"])["superseded_by"] == second["job_id"]
    assert queue.read(second["job_id"])["resume_from"] == attempt["attempt_id"]
    assert audits.get(store, attempt["attempt_id"])["state"] == "superseded"


def stored_full(world, route, *, catalog=None, age_days=0):
    store = Store(world.state)
    repository = audits.select_repository(route.policy)
    identity = audits.audit_identity(route.policy.audit, "full", repository, world.base)
    if catalog:
        identity = {**identity, "catalog_digest": catalog}
    attempt, _ = audits.begin(store, identity)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE audits SET state='complete', completed_at=?, report_json=? WHERE attempt_id=?",
            (
                time.time() - age_days * 86400,
                json.dumps(build_report("full", world.base)),
                attempt["attempt_id"],
            ),
        )


def schedule(world, route, kind="schedule"):
    return [
        (item["tier"], item["reason"])
        for item in request_audits(
            world.state, route, {"kind": kind}, default_head=lambda: ("main", world.head)
        )
    ]


def test_catalog_change_triggers_a_new_baseline(world):
    route, _ = world.make()
    stored_full(world, route, catalog="0" * 64)
    assert schedule(world, route) == [("full", "catalog")]
    [job] = AuditQueue(world.state).jobs()
    assert job["scope"] == "repository" and job["identity"]["head"] == world.head
    assert job["identity"]["catalog_digest"] == audits.vendored_identity()["catalog_digest"]


def test_schedule_and_age_triggers(world):
    route, _ = world.make()
    assert schedule(world, route, "registered") == [("full", "baseline")]
    store_baseline(world, route)
    assert schedule(world, route, "registered") == []
    assert schedule(world, route) == []
    stored_full(world, route, age_days=8)
    assert schedule(world, route) == [("full", "schedule")]
    stored_full(world, route, age_days=15)
    assert schedule(world, route) == [("full", "stale")]


def test_worker_registers_then_ticks_hourly(world, monkeypatch):
    from cirra import worker

    route, _ = world.make()
    monkeypatch.setattr(worker, "default_branch_head", lambda route, reader: ("main", world.base))
    processor = worker.Processor(world.state, lambda name: None, None)
    routes = {"main": route}
    first = processor.schedule_audits(routes, now=0)
    assert [(item["tier"], item["reason"], item["attached"]) for item in first] == [
        ("full", "baseline", False)
    ]
    assert processor.schedule_audits(routes, now=10) == []
    later = processor.schedule_audits(routes, now=worker.AUDIT_SCHEDULE_SECONDS + 1)
    assert [(item["reason"], item["attached"]) for item in later] == [("baseline", True)]
    assert len(AuditQueue(world.state).jobs()) == 1


def test_packet_lists_domains_only_for_full_audits(world):
    route, _ = world.make()
    store = Store(world.state)
    repository = audits.select_repository(route.policy)
    full, _ = audits.begin(
        store, audits.audit_identity(route.policy.audit, "full", repository, world.base)
    )
    pr, _ = audits.begin(
        store,
        audits.audit_identity(route.policy.audit, "pr", repository, world.head, world.base),
        "change:7",
    )
    assert len(audits.make_packet(store, full, world.origin.path)["domains"]) == 10
    assert audits.make_packet(store, pr, world.origin.path)["domains"] == []


def test_checkpoint_validation(tmp_path):
    checkpoint(tmp_path, "security", ["a.py"])
    assert audits.valid_checkpoint(tmp_path / "security.json")["domain"] == "security"
    (tmp_path / "data-and-storage.json").write_text(
        json.dumps({"domain": "data-and-storage", "coverage": [{"id": "C011"}]}), encoding="utf-8"
    )
    assert audits.valid_checkpoint(tmp_path / "data-and-storage.json") is None
    (tmp_path / "unknown.json").write_text(
        json.dumps({"domain": "unknown", "coverage": []}), encoding="utf-8"
    )
    assert audits.valid_checkpoint(tmp_path / "unknown.json") is None
