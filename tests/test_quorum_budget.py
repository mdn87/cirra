"""Live queue entry points against the same authenticated-CAS contract as the voting service."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from audit_fixtures import audit_policy

from cirra import audits, quorum_budget
from cirra.audit_worker import audit_once
from cirra.domain import CirraError, ProviderRateLimited
from cirra.job_queue import AuditQueue, Queue
from cirra.publish import write_receipt
from cirra.quorum import OWNER_KEY, Owner, OwnershipLost, QuorumUnavailable, bind_owner
from cirra.store import Store
from cirra.worker import work_once


class Voters:
    def __init__(self):
        self.state = {}
        self.lock = threading.Lock()
        self.config = SimpleNamespace(node="primary", preferred_node="primary")
        self.lose_admission_reply = False
        self.lose_seed_reply = False

    def owner(self):
        owner = Owner(self)
        self.state[OWNER_KEY] = owner.token
        return owner

    def get(self, key):
        with self.lock:
            return self.state.get(key)

    def compare(self, checks, puts, *, deletes=()):
        with self.lock:
            if any(self.state.get(k) != v for k, v in checks.items()):
                return False
            self.state.update(puts)
            if self.lose_seed_reply and quorum_budget.EPOCH in puts:
                self.lose_seed_reply = False
                raise QuorumUnavailable("initial seed reply lost")
            if self.lose_admission_reply and any('"phase":"charged"' in v for v in puts.values()):
                self.lose_admission_reply = False
                raise QuorumUnavailable("admission reply lost")
            return True


@pytest.fixture
def primary(tmp_path):
    voters = Voters()
    return voters, voters.owner(), tmp_path


def review_queue(root, suffix):
    queue = Queue(root)
    route = SimpleNamespace(name="test", digest="approved")
    queue.enqueue(route, {"delivery": suffix, "payload_digest": suffix, "source": "manual"})
    config = SimpleNamespace(
        routes={route.name: route}, max_attempts=3, max_jobs_per_day=1, retry_seconds=1
    )
    return queue, config


def audit_queue(root, config, revision):
    store = Store(root)
    queue = AuditQueue(root, store)
    repository = audits.select_repository(config)
    identity = audits.audit_identity(config.audit, "pr", repository, f"{revision:040x}", "f" * 40)
    job = queue.request(identity, f"change:{revision}", reason="change")
    return queue, store, job["job_id"]


def session(*args):
    return {"state": "awaiting_import"}


@pytest.mark.parametrize("failure", [OwnershipLost, QuorumUnavailable])
def test_worker_abandons_pass_without_retry_write_on_ownership_failure(primary, failure):
    voters, owner, root = primary
    queue, config = review_queue(root, "lost-owner")
    quorum_budget.seed_budgets(owner, root, quorum_budget.utc_day(), 1, 2)

    def process(*_):
        raise failure("cannot confirm owner")

    with bind_owner(owner), pytest.raises(failure):
        work_once(config, queue, process)
    assert queue.scan()[0][0]["state"] == "running"


def test_branch_ci_takeover_consumes_new_budget_without_repeating_model_reviews(primary):
    voters, owner, root = primary
    queue, config = review_queue(root, "branch-ci")
    route = config.routes["test"]
    route.branches, route.observer = ("main",), "observer.yaml"
    config.max_jobs_per_day = 2
    job = queue.scan()[0][0]
    job["hint"].update(source="branch", branch="main", change="branch:main")
    queue.save(job)
    quorum_budget.seed_budgets(owner, root, quorum_budget.utc_day(), 2, 2)
    with bind_owner(owner):
        assert queue.begin_review(job, 2) is not None  # Existing pre-upgrade charge.
    # A stale restore does not know whether the old observer finished.
    job.update(state="running", attempts=1)
    queue.save(job)
    replacement = voters.owner()
    with bind_owner(replacement):
        result = work_once(config, queue, lambda *_: {"state": "done", "review": "disabled"})
    assert result["state"] == "done"
    assert usage(root, "review", quorum_budget.utc_day()) == 2


def usage(root, kind, day):
    path = root / {"review": "worker-usage", "audit": "audit-usage"}[kind] / f"{day}.json"
    return json.loads(path.read_text())["attempts"]


def test_seed_requires_authoritative_primary_and_pins_both_counts(primary):
    voters, owner, root = primary
    day = quorum_budget.utc_day()
    root.mkdir(exist_ok=True)
    write_receipt(root / "worker-usage" / f"{day}.json", {"attempts": 3})
    write_receipt(root / "audit-usage" / f"{day}.json", {"attempts": 2, "maximum": 2})
    queue, config = review_queue(root, "seed")
    with bind_owner(owner), pytest.raises(CirraError, match="seeded"):
        work_once(config, queue, lambda *_: pytest.fail("unseeded"))
    voters.config.node = "standby"
    with pytest.raises(CirraError, match="preferred"):
        quorum_budget.seed_budgets(owner, root, day, 1, 2)
    voters.config.node = "primary"
    seeded = quorum_budget.seed_budgets(owner, root, day, 1, 2)
    assert seeded == {
        "review": {"attempts": 3, "maximum": 1},
        "audit": {"attempts": 2, "maximum": 2},
    }
    assert quorum_budget.seed_budgets(owner, root, day, 1, 2) == seeded
    with pytest.raises(CirraError, match="policy"):
        quorum_budget.seed_budgets(owner, root, day, 3, 2)
    with bind_owner(owner):
        assert work_once(config, queue, lambda *_: pytest.fail("existing usage was reset")) is None
        config.max_jobs_per_day = 9
        with pytest.raises(CirraError, match="limit"):
            work_once(config, queue, lambda *_: pytest.fail("widened standby policy"))


@pytest.mark.parametrize("kind", ["review", "audit"])
def test_racing_workers_share_one_remaining_slot(kind, primary, policy_data):
    _, owner, root = primary
    policy = audit_policy(root, policy_data)
    day = quorum_budget.utc_day()
    quorum_budget.seed_budgets(owner, root, day, 1, 1)
    go = threading.Barrier(2)
    roots = [root / "copy-1", root / "copy-2"]

    def run(i):
        local = roots[i]
        if kind == "review":
            q, cfg = review_queue(local, str(i))

            def operation():
                return work_once(cfg, q, lambda *_: {"state": "done"})
        else:
            q, store, _ = audit_queue(local, policy, i + 1)

            def operation():
                return audit_once(q, store, 1, session)

        with bind_owner(owner):
            go.wait(timeout=10)
            return operation()

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(run, (0, 1)))
    assert sum(job is not None for job in result) == 1
    assert all(usage(local, kind, day) == 1 for local in roots)
    assert all(
        key == OWNER_KEY or key.startswith("budgets/") and len(key) == 72
        for key in owner.quorum.state
    )


@pytest.mark.parametrize("kind", ["review", "audit"])
def test_committed_admission_with_lost_reply_never_redispatches_on_stale_copy(
    kind, primary, policy_data, monkeypatch
):
    voters, owner, root = primary
    policy = audit_policy(root, policy_data)
    day = quorum_budget.utc_day()
    quorum_budget.seed_budgets(owner, root, day, 1, 1)
    primary_root, stale_root = root / "original", root / "restored"
    if kind == "review":
        qa, cfg = review_queue(primary_root, "same")
        qb, _ = review_queue(stale_root, "same")

        def original():
            return work_once(cfg, qa, lambda *_: pytest.fail("lost reply"))

        def restored():
            return work_once(cfg, qb, lambda *_: pytest.fail("duplicate review"))

        def check():
            return qb.status()[0]["state"]
    else:
        qa, sa, _ = audit_queue(primary_root, policy, 1)
        qb, sb, job = audit_queue(stale_root, policy, 1)

        def original():
            return audit_once(qa, sa, 1, lambda *_: pytest.fail("lost reply"))

        def restored():
            return audit_once(
                qb,
                sb,
                1,
                lambda *_: pytest.fail("duplicate audit"),
                now=datetime.combine(quorum_budget.utc_day(), datetime.max.time(), UTC).timestamp(),
            )

        def check():
            return qb.read(job)["state"]

    voters.lose_admission_reply = True
    with bind_owner(owner), pytest.raises(QuorumUnavailable, match="reply lost"):
        original()
    new_owner = voters.owner()
    tomorrow = day + timedelta(days=1)
    monkeypatch.setattr(quorum_budget, "utc_day", lambda: tomorrow)
    with bind_owner(new_owner):
        restored()
        assert check() == "attention"
        assert quorum_budget.sync_usage(new_owner, stale_root, tomorrow)[kind]["attempts"] == 0
    assert json.loads(voters.get(quorum_budget._bucket(kind, day)))["attempts"] == 1


def test_preflight_review_refund_retries_with_new_ticket_and_original_day(primary, monkeypatch):
    voters, owner, root = primary
    day = quorum_budget.utc_day()
    quorum_budget.seed_budgets(owner, root, day, 1, 1)
    with bind_owner(owner):
        q, cfg = review_queue(root, "preflight")

    def refuse(*_):
        error = ProviderRateLimited("preflight", 0)
        error.before_execution = True
        raise error

    with bind_owner(owner):
        first = work_once(cfg, q, refuse)
        assert (
            first["state"] == "retry" and first["attempts"] == 0 and usage(root, "review", day) == 0
        )
        ticket = quorum_budget.operation("review", first["id"], 0)
        assert not q.return_unused_attempt((day, ticket))
        first["not_before"] = 0
        q.save(first)
        monkeypatch.setattr(q, "cooldown", lambda *a: 0)
        assert work_once(cfg, q, lambda *_: {"state": "done"})["state"] == "done"
        assert usage(root, "review", day) == 1
    other = voters.owner()
    ticket = quorum_budget.operation("review", first["id"], 1)
    with bind_owner(other):
        assert not q.return_unused_attempt((day, ticket))
        assert usage(root, "review", day) == 1
    with bind_owner(owner), pytest.raises(OwnershipLost):
        q.return_unused_attempt((day, ticket))


def test_audit_refunds_only_a_proven_unused_admission_and_reopens_deferred_work(
    primary, policy_data, monkeypatch
):
    _, owner, root = primary
    cfg = audit_policy(root, policy_data)
    day = quorum_budget.utc_day()
    q, store, first = audit_queue(root, cfg, 1)
    _, _, second = audit_queue(root, cfg, 2)
    quorum_budget.seed_budgets(owner, root, day, 1, 1)
    claim = q.next_job

    def claim_then_cancel(maximum, now=None):
        job = claim(maximum, now)
        if job and job["id"] == first:
            assert claim(maximum, now) is None
            assert q.read(second)["state"] == "deferred"
            q.cancel(first, "pre-run cancellation")
        return job

    with bind_owner(owner):
        monkeypatch.setattr(q, "next_job", claim_then_cancel)
        cancelled = audit_once(q, store, 1, lambda *_: pytest.fail("cancelled"))
        assert cancelled["state"] == "cancelled" and usage(root, "audit", day) == 0
        assert not q.return_unused_admission(cancelled)
        assert audit_once(q, store, 1, session)["id"] == second
        assert usage(root, "audit", day) == 1


def test_rollover_preserves_previous_charge_and_cannot_reopen_a_clock_day(primary, monkeypatch):
    _, owner, root = primary
    day = quorum_budget.utc_day()
    tomorrow = day + timedelta(days=1)
    quorum_budget.seed_budgets(owner, root, day, 1, 1)
    with bind_owner(owner):
        q, cfg = review_queue(root, "older")
        reservation = q.begin_review(q.pending()[0], 1)
        monkeypatch.setattr(quorum_budget, "utc_day", lambda: tomorrow)
        other, cfg2 = review_queue(root / "other", "new")
        assert work_once(cfg2, other, lambda *_: {"state": "done"})["state"] == "done"
        assert q.return_unused_attempt(reservation) and not q.return_unused_attempt(reservation)
        assert usage(root, "review", day) == 0 and usage(root / "other", "review", tomorrow) == 1
        monkeypatch.setattr(quorum_budget, "utc_day", lambda: day)
        with pytest.raises(CirraError, match="behind"):
            q.usage()


def test_missing_quorum_records_or_owner_cannot_reopen_local_budgets(primary, policy_data):
    voters, owner, root = primary
    cfg = audit_policy(root, policy_data)
    day = quorum_budget.utc_day()
    q, settings = review_queue(root, "offline")
    audit, _, _ = audit_queue(root, cfg, 1)
    quorum_budget.seed_budgets(owner, root, day, 1, 1)
    with pytest.raises(CirraError, match="current publishing owner"):
        work_once(settings, q, lambda *_: pytest.fail("local review bypass"))
    with pytest.raises(CirraError, match="current publishing owner"):
        audit.next_job(1)
    del voters.state[quorum_budget.EPOCH]
    with pytest.raises(CirraError, match="previously seeded"):
        quorum_budget.seed_budgets(owner, root, day, 1, 1)


def test_audit_checkpoint_resumes_free_today_charges_tomorrow_and_holds_lost_runner(
    primary, policy_data, monkeypatch
):
    _, owner, root = primary
    cfg = audit_policy(root, policy_data)
    q, store, identifier = audit_queue(root, cfg, 1)
    day = quorum_budget.utc_day()
    quorum_budget.seed_budgets(owner, root, day, 1, 1)
    with bind_owner(owner):
        first = q.next_job(1)
        q.update(identifier, state="queued", preemptions=1)
        second = q.next_job(1)
        assert first["budget_sequence"] == 1 and second["budget_sequence"] == 2
        assert not q.return_unused_admission(second) and usage(root, "audit", day) == 1
        q.update(identifier, state="queued", preemptions=2)
        tomorrow = day + timedelta(days=1)
        monkeypatch.setattr(quorum_budget, "utc_day", lambda: tomorrow)
        now = datetime.combine(tomorrow, datetime.min.time(), UTC).timestamp()
        assert q.next_job(1, now)["admitted_on"] == tomorrow.isoformat()
        assert usage(root, "audit", tomorrow) == 1
        assert (
            audit_once(q, store, 1, lambda *_: pytest.fail("lost audit ran again"), now=now) is None
        )
        assert q.read(identifier)["state"] == "attention"
        assert usage(root, "audit", tomorrow) == 1


def test_stale_shared_inbox_delivery_observes_settlement_before_spending(primary):
    _, owner, root = primary
    day = quorum_budget.utc_day()
    quorum_budget.seed_budgets(owner, root, day, 1, 1)
    with bind_owner(owner):
        q, settings = review_queue(root, "settled")
        q.shared_inbox = SimpleNamespace(require_pending=lambda current, job: False)
        assert (
            work_once(settings, q, lambda *_: pytest.fail("already settled"))["state"] == "skipped"
        )
        assert q.usage()["attempts"] == 0


def test_enrollment_preflight_and_completion_run_under_both_admission_locks(primary):
    voters, owner, root = primary
    day = quorum_budget.utc_day()
    calls = []

    def before():
        assert voters.get(quorum_budget.EPOCH) is None
        calls.append("before")
        raise CirraError("unfinished legacy work")

    def after():
        with pytest.raises(CirraError, match="owns this change"):
            with quorum_budget.enrollment_locks(root):
                pytest.fail("enrollment changed concurrently")
        assert voters.get(quorum_budget.EPOCH) is not None
        assert (root / "quorum-budget-seed.json").is_file()
        calls.append("after")

    with pytest.raises(CirraError, match="unfinished legacy"):
        quorum_budget.seed_budgets(owner, root, day, 1, 1, before_seed=before, after_seed=after)
    assert not (root / "worker-queue-format.json").exists()
    assert voters.get(quorum_budget.EPOCH) is None
    quorum_budget.seed_budgets(owner, root, day, 1, 1, after_seed=after)
    assert calls == ["before", "after"]


def test_unacknowledged_seed_resumes_next_day_without_refilling_either_budget(primary, monkeypatch):
    voters, owner, root = primary
    day = quorum_budget.utc_day()
    tomorrow = day + timedelta(days=1)
    write_receipt(root / "worker-usage" / f"{day}.json", {"attempts": 1})
    write_receipt(root / "audit-usage" / f"{day}.json", {"attempts": 2})
    voters.lose_seed_reply = True
    with pytest.raises(QuorumUnavailable, match="initial seed reply lost"):
        quorum_budget.seed_budgets(owner, root, day, 2, 2)
    original = json.loads(voters.get(quorum_budget.EPOCH))["seed"]
    assert json.loads((root / "quorum-budget-seed.json").read_text())["seed"] == original
    with bind_owner(owner):
        late, settings = review_queue(root, "before-midnight")
        settings.max_jobs_per_day = 2
        assert work_once(settings, late, lambda *_: {"state": "done"})
        monkeypatch.setattr(quorum_budget, "utc_day", lambda: tomorrow)
        today, settings = review_queue(root / "restored", "after-midnight")
        settings.max_jobs_per_day = 2
        assert work_once(settings, today, lambda *_: {"state": "done"})
    assert usage(root, "review", day) == usage(root, "audit", day) == 2
    with pytest.raises(CirraError, match="policy"):
        quorum_budget.seed_budgets(owner, root, tomorrow, 4, 2)
    for candidate in (root, root / "restored"):
        totals = quorum_budget.seed_budgets(owner, candidate, tomorrow, 2, 2)
        assert totals == {
            "review": {"attempts": 1, "maximum": 2},
            "audit": {"attempts": 0, "maximum": 2},
        }
        assert json.loads((candidate / "quorum-budget-seed.json").read_text())["seed"] == original
    assert json.loads(voters.get(quorum_budget._bucket("review", day)))["attempts"] == 2
    assert json.loads(voters.get(quorum_budget._bucket("audit", day)))["attempts"] == 2
