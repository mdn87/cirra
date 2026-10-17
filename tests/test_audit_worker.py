"""Stage 4 of the production-audit tiers plan: pr audits through the worker."""

import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from audit_fixtures import Repository, audit_policy, build_report, lines, write_report

from cirra import audits
from cirra.audit_worker import (
    AuditCheckPublisher,
    AuditPublication,
    AuditRunner,
    audit_once,
    request_audits,
)
from cirra.domain import CirraError
from cirra.job_queue import AuditQueue
from cirra.store import Store

TESTS = Path(__file__).resolve().parent

AGENT = """
import json, os, sys
from pathlib import Path
sys.path.insert(0, {tests!r})
from audit_fixtures import build_report, write_report
packet = json.loads(Path(os.environ["CIRRA_AUDIT_PACKET"]).read_text(encoding="utf-8"))
gaps = {gaps!r}
report = build_report(
    packet["tier"], packet["repository"]["head"], packet["repository"]["base"],
    gaps=[tuple(gap) for gap in gaps], repo_path=os.environ["CIRRA_AUDIT_CHECKOUT"],
)
write_report(Path(os.environ["CIRRA_AUDIT_DIR"]), report)
"""

WAITER = """
import os, sys, time
from pathlib import Path
stop = Path(os.environ["CIRRA_AUDIT_DIR"]) / "stop"
for _ in range(400):
    if stop.exists():
        sys.exit(0)
    time.sleep(0.05)
sys.exit(9)
"""


class FakeGitHub:
    def __init__(self, app_id=5, existing=()):
        self.app_id, self.runs, self.posts = app_id, list(existing), []

    def request(self, method, route, params=None, body=None):
        if method == "GET":
            return {"check_runs": self.runs}, False
        self.posts.append(body)
        item = {**body, "id": 100 + len(self.posts), "app": {"id": self.app_id}}
        self.runs.append(item)
        return item, False


@pytest.fixture
def world(tmp_path, policy_data):
    origin = Repository(tmp_path / "origin")
    base = origin.commit({"app.py": lines(20), "pyproject.toml": "[project]\n"})
    origin.git("switch", "-c", "feature")
    head = origin.commit({"app.py": lines(10) + lines(12, "changed")})
    origin.git("update-ref", "refs/pull/7/head", head)
    origin.git("switch", "main")
    state = tmp_path / "state"

    def make(runner=None, gate="critical", **limits):
        audit = {
            "tiers": ["pr", "release", "full"],
            "gates": {"pr": gate},
            "limits": {"debounce_seconds": 0, **limits},
        }
        if runner:
            audit["runner"] = runner
        config = audit_policy(
            tmp_path, policy_data, audit, f"policy-{len(list(tmp_path.glob('policy-*')))}.yaml"
        )
        route = SimpleNamespace(
            name="main",
            provider="github",
            instance="https://github.com",
            repository="example/repo",
            repository_id=config.repositories[0]["repository_id"],
            policy=config,
            reader_env=None,
            publisher={"app_id": 5},
        )
        runner_object = AuditRunner(
            state,
            {"main": route},
            lambda name: None,
            url_for=lambda _: str(origin.path),
            protocols=("file",),
            poll_seconds=0.05,
            grace_seconds=2,
        )
        return route, runner_object

    return SimpleNamespace(origin=origin, base=base, head=head, state=state, make=make)


def agent(tmp_path, gaps=(), source=AGENT):
    script = tmp_path / f"agent-{time.monotonic_ns()}.py"
    script.write_text(source.format(tests=str(TESTS), gaps=list(gaps)), encoding="utf-8")
    return {"kind": "command", "command": [sys.executable, str(script)]}


def store_baseline(world, route, gaps=()):
    """A complete repository-scoped full audit of the default branch."""
    store = Store(world.state)
    repository = audits.select_repository(route.policy)
    identity = audits.audit_identity(route.policy.audit, "full", repository, world.base)
    attempt, _ = audits.begin(store, identity)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE audits SET state='complete', completed_at=?, report_json=?, "
            "comparison_json=? WHERE attempt_id=?",
            (
                time.time(),
                json.dumps(build_report("full", world.base, gaps=gaps)),
                json.dumps({"new": [], "open": [], "resolved": []}),
                attempt["attempt_id"],
            ),
        )


def change_event(*paths):
    return {"kind": "change", "changed_paths": list(paths)}


def request(world, route, *paths):
    return request_audits(
        world.state,
        route,
        change_event(*paths),
        head=world.head,
        base=world.base,
        change=7,
        default_head=lambda: ("main", world.base),
    )


def test_change_without_baseline_queues_pr_and_a_default_branch_baseline(world):
    route, _ = world.make()
    receipts = request(world, route, "app.py")
    assert [(item["tier"], item["reason"]) for item in receipts] == [
        ("pr", "change"),
        ("full", "baseline"),
    ]
    jobs = {job["tier"]: job for job in AuditQueue(world.state).jobs()}
    assert jobs["pr"]["scope"] == "change:7"
    assert jobs["pr"]["identity"]["base"] == world.base
    assert jobs["pr"]["context"]["ref"] == "refs/pull/7/head"
    assert jobs["full"]["scope"] == "repository"
    assert jobs["full"]["identity"]["head"] == world.base
    assert jobs["full"]["context"]["ref"] == "refs/heads/main"


def test_structural_change_queues_a_change_scoped_full_audit(world):
    route, _ = world.make()
    store_baseline(world, route)
    receipts = request(world, route, "pyproject.toml")
    assert [(item["tier"], item["reason"]) for item in receipts] == [
        ("pr", "change"),
        ("full", "structural"),
    ]
    full = next(job for job in AuditQueue(world.state).jobs() if job["tier"] == "full")
    assert full["scope"] == "change:7" and full["identity"]["head"] == world.head


def test_policy_without_audit_requests_nothing(world, inputs):
    route, _ = world.make()
    _, plain = inputs()
    route.policy = plain
    assert request(world, route, "app.py") == []


def test_command_audit_publishes_once_and_redelivery_does_not_republish(world, tmp_path):
    route, runner = world.make(agent(tmp_path, [("C011", "app.py", 11, 12, "critical")]))
    store_baseline(world, route)
    request(world, route, "app.py")
    store = Store(world.state)
    queue = AuditQueue(world.state, store)
    github = FakeGitHub()
    publication = AuditPublication(
        world.state,
        {"main": route},
        lambda r: AuditCheckPublisher(r.repository, 5, "t", http=github),
    )
    job = audit_once(queue, store, 20, runner, on_complete=publication)
    assert job["state"] == "done", job
    assert job["result"]["publication"]["conclusion"] == "failure"
    assert job["result"]["publication"]["gate"] == "block"
    [posted] = github.posts
    assert posted["head_sha"] == world.head and posted["name"] == "cirra/audit"
    assert posted["external_id"] == job["audit_id"]
    assert r"\[C011\] Fixture gap" in posted["output"]["summary"]
    # The webhook is redelivered: the request attaches and nothing is republished.
    [again] = [item for item in request(world, route, "app.py") if item["tier"] == "pr"]
    assert again == {"tier": "pr", "reason": "change", "job_id": job["id"], "attached": True}
    assert audit_once(queue, store, 20, runner, on_complete=publication) is None
    assert len(github.posts) == 1
    runner.cleanup(store)
    assert not (runner.attempt_dir(job["attempt_id"]) / "checkout").exists()


def test_existing_findings_do_not_fail_the_pr_gate(world, tmp_path):
    # The baseline already has C011 in app.py: the pull request is not blamed for it.
    gap = ("C011", "app.py", 11, 12, "critical")
    route, runner = world.make(agent(tmp_path, [gap]), gate="critical")
    store_baseline(world, route, [("C011", "app.py", 1, 2, "critical")])
    request(world, route, "app.py")
    store = Store(world.state)
    github = FakeGitHub()
    publication = AuditPublication(
        world.state,
        {"main": route},
        lambda r: AuditCheckPublisher(r.repository, 5, "t", http=github),
    )
    job = audit_once(AuditQueue(world.state, store), store, 20, runner, on_complete=publication)
    assert job["result"]["comparison"]["open"] == ["C011:app.py"]
    assert job["result"]["publication"]["conclusion"] == "success"


def test_session_audit_waits_for_import_then_publishes(world, tmp_path):
    route, runner = world.make()
    store_baseline(world, route)
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
    assert waiting["state"] == "awaiting_import"
    packet = json.loads(Path(waiting["packet_path"]).read_text(encoding="utf-8"))
    checkout = Path(packet["repository"]["path"])
    assert (checkout / "app.py").read_text(encoding="utf-8").endswith("changed 12\n")
    report = build_report("pr", world.head, world.base, repo_path=checkout)
    audits.import_report(
        store, waiting["attempt_id"], write_report(tmp_path / "out", report), checkout
    )
    assert audit_once(queue, store, 20, runner, on_complete=publication) is None
    settled = queue.read(waiting["id"])
    assert settled["state"] == "done"
    assert settled["result"]["publication"]["conclusion"] == "success"
    assert len(github.posts) == 1


def test_superseded_command_audit_is_stopped_at_a_checkpoint(world, tmp_path):
    route, runner = world.make(agent(tmp_path, source=WAITER))
    store_baseline(world, route)
    request(world, route, "app.py")
    store = Store(world.state)
    queue = AuditQueue(world.state, store)
    calls = []

    def stop():
        calls.append(1)
        return "superseded" if len(calls) > 2 else None

    job = queue.next_job(20)
    attempt, _ = audits.begin(store, job["identity"], job["scope"])
    outcome = runner(job, attempt, stop)
    assert outcome == {"state": "stopped", "reason": "superseded"}
    work = runner.attempt_dir(attempt["attempt_id"]) / "work"
    assert (work / "stop").read_text(encoding="utf-8") == "superseded"


def test_agent_failure_and_timeout_fail_the_job(world, tmp_path):
    failing = agent(tmp_path, source="import sys\nsys.exit(3)\n")
    route, runner = world.make(failing)
    store_baseline(world, route)
    request(world, route, "app.py")
    store = Store(world.state)
    job = audit_once(AuditQueue(world.state, store), store, 20, runner)
    assert job["state"] == "failed" and "exited with status 3" in job["error"]

    slow = agent(tmp_path, source="import time\ntime.sleep(30)\n")
    route, runner = world.make(slow)
    route.policy = replace(
        route.policy, audit=replace(route.policy.audit, audit_timeout_seconds=0.3)
    )
    attempt_job = AuditQueue(world.state, store).request(
        audits.audit_identity(
            route.policy.audit, "full", audits.select_repository(route.policy), world.head
        ),
        "change:7",
        reason="structural",
        context={"route": "main", "change": 7, "ref": "refs/pull/7/head"},
    )
    job = audit_once(AuditQueue(world.state, store), store, 20, runner)
    assert job["id"] == attempt_job["job_id"]
    assert job["state"] == "failed" and "audit_timeout_seconds" in job["error"]


def test_publisher_reuses_an_existing_owned_check_run(world):
    existing = {
        "id": 9,
        "name": "cirra/audit",
        "head_sha": world.head,
        "external_id": "a" * 64,
        "app": {"id": 5},
    }
    github = FakeGitHub(existing=[existing, {**existing, "id": 10, "app": {"id": 6}}])
    result = AuditCheckPublisher("example/repo", 5, "t", http=github).publish(
        world.head, "a" * 64, "success", "summary"
    )
    assert result == {"native_id": "9", "existing": True} and github.posts == []


def test_failed_publication_puts_the_job_in_attention(world, tmp_path):
    route, runner = world.make(agent(tmp_path))
    store_baseline(world, route)
    request(world, route, "app.py")
    store = Store(world.state)

    class Broken:
        def publish(self, *args):
            raise CirraError("GitHub refused the check run")

    publication = AuditPublication(world.state, {"main": route}, lambda r: Broken())
    job = audit_once(AuditQueue(world.state, store), store, 20, runner, on_complete=publication)
    assert job["state"] == "attention"
    assert job["result"]["publication"]["reason"] == "GitHub refused the check run"

    from cirra.health import check

    assert any(problem["kind"] == "audit" for problem in check(world.state)["problems"])


def test_worker_loop_runs_audit_passes_and_survives_their_errors(tmp_path, capsys):
    from cirra.worker import run_worker

    class Process:
        def __init__(self):
            self.calls = 0

        def __call__(self, route, hint):
            raise AssertionError("no review jobs are queued")

        def audit(self, config):
            self.calls += 1
            raise CirraError("default branch read failed")

    config = SimpleNamespace(
        max_jobs=10, routes={}, poll_seconds=0.01, max_attempts=3, max_jobs_per_day=30
    )
    process = Process()
    assert run_worker(config, tmp_path, process, once=True) is None
    assert process.calls == 1
    assert '"audit_error": "default branch read failed"' in capsys.readouterr().out


def test_publication_is_recorded_once_per_identity(world, tmp_path):
    # A crash between publishing and saving the job must not republish on recovery.
    route, runner = world.make(agent(tmp_path))
    store_baseline(world, route)
    request(world, route, "app.py")
    store = Store(world.state)
    job = audit_once(AuditQueue(world.state, store), store, 20, runner)
    github = FakeGitHub(existing=[])
    publication = AuditPublication(
        world.state,
        {"main": route},
        lambda r: AuditCheckPublisher(r.repository, 5, "t", http=github),
    )
    attempt = audits.get(store, job["attempt_id"])
    first = publication(job, attempt)
    github.runs.clear()  # Even if the remote read misses it, the receipt prevents a repost.
    second = publication(job, attempt)
    assert first["state"] == "published" and second["duplicate"] is True
    assert len(github.posts) == 1
