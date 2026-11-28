"""Native operator actions use the original host and checkpoint partial failures."""

import argparse
import json
import sqlite3
from types import SimpleNamespace

import pytest
from audit_fixtures import audit_policy
from test_audit_release import candidate as candidate_fixture
from test_audit_release import store_release_audit
from test_publication_fence import Cluster

from cirra import audits, coordinated_ops
from cirra.data_authority import AUTHORITY, DataAuthority
from cirra.domain import CirraError, digest
from cirra.job_queue import AuditQueue
from cirra.locking import change_lock
from cirra.quorum import OWNER_KEY, QuorumUnavailable
from cirra.store import Store

release_candidate = candidate_fixture


@pytest.fixture
def operator(tmp_path, policy_data, monkeypatch):
    state = tmp_path / "current.restores" / ("a" * 64)
    store = Store(state)
    selector = tmp_path / "current"
    try:
        selector.symlink_to(state, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    policy = audit_policy(tmp_path, policy_data)
    repository = audits.select_repository(policy)
    route = SimpleNamespace(policy=policy, **repository)
    cluster = Cluster()
    cluster.config = SimpleNamespace(
        node="linux", preferred_node="linux", standby_node="mac", lease_seconds=3600
    )
    owners = []

    def acquire():
        if cluster.get(OWNER_KEY):
            return None
        owner = cluster.elect()
        owners.append(owner)
        return owner

    cluster.acquire = acquire
    inbox = SimpleNamespace(
        quorum=cluster,
        queue=SimpleNamespace(directory=state / "queue"),
        opaque=digest,
        check=lambda: None,
    )
    initial = cluster.elect()
    authority = DataAuthority(inbox)
    authority.initialize(initial)
    cluster.compare({OWNER_KEY: initial.token}, {}, deletes=(OWNER_KEY,))
    monkeypatch.setattr(coordinated_ops, "connect", lambda *args: inbox)
    config = SimpleNamespace(routes={"repo": route})

    def args(command="request-audit", **overrides):
        result = argparse.Namespace(
            command=command,
            state_dir=selector,
            config=policy.path,
            repository_id=None,
            tier="full",
            head="b" * 40,
            base=None,
            ref="main",
            change=None,
            reason="operator disposition",
            job=None,
        )
        for name, value in overrides.items():
            setattr(result, name, value)
        return result

    def run(command="request-audit", **overrides):
        return coordinated_ops.operate(args(command, **overrides), config, lambda _: None, None)

    return SimpleNamespace(
        state=state,
        store=store,
        selector=selector,
        cluster=cluster,
        owners=owners,
        authority=authority,
        config=config,
        args=args,
        run=run,
    )


def test_request_replay_and_cancel_use_native_queue_without_duplicate_admission(operator):
    first = operator.run()
    assert first["state"] == "complete" and first["authority_checkpointed"]
    job_id = first["result"]["job_id"]
    assert operator.run()["result"] == {"job_id": job_id, "attached": True}
    assert len(list(AuditQueue(operator.state).jobs())) == 1
    assert not any(key.startswith("budgets/") for key in operator.cluster.values)
    result = operator.run("cancel-audit", job=job_id)
    assert result["state"] == "complete" and result["result"]["state"] == "cancelled"
    assert result["authority_checkpointed"]


@pytest.mark.parametrize("failure", ["worker_lock", "lease", "standby", "frozen", "selector"])
def test_operator_refuses_before_native_writes(operator, failure):
    if failure == "worker_lock":
        with change_lock(operator.state / "queue" / "locks", "worker", 0):
            with pytest.raises(CirraError):
                operator.run()
    else:
        if failure == "lease":
            operator.cluster.elect()
        elif failure == "standby":
            operator.cluster.config.node = "mac"
        elif failure == "frozen":
            owner = operator.cluster.elect()
            operator.authority.freeze(owner, "mac")
            operator.cluster.compare({OWNER_KEY: owner.token}, {}, deletes=(OWNER_KEY,))
        elif failure == "selector":
            operator.selector.rename(operator.selector.with_name("retained-selector"))
        with pytest.raises((CirraError, OSError)):
            operator.run()
    assert not list((operator.state / "audit-queue").glob("*.json"))


def test_expiry_after_native_write_reports_unconfirmed_and_replay_attaches(operator, monkeypatch):
    original = coordinated_ops.audit_cli.run

    def expire(args):
        code = original(args)
        operator.owners[-1].deadline = 0
        return code

    monkeypatch.setattr(coordinated_ops.audit_cli, "run", expire)
    receipt = operator.run()
    assert receipt["state"] == "outcome_unconfirmed" and receipt["exit_code"] == 2
    assert receipt["authority_checkpointed"] is False
    [job] = AuditQueue(operator.state).jobs()
    monkeypatch.setattr(coordinated_ops.audit_cli, "run", original)
    assert operator.run()["result"] == {"job_id": job["id"], "attached": True}


def test_checkpoint_lost_reply_recovers_native_intent(operator, monkeypatch):
    original = operator.cluster.compare
    lose = []

    def compare(checks, puts, **kwargs):
        result = original(checks, puts, **kwargs)
        if result and AUTHORITY in puts:
            lose.append(puts[AUTHORITY])
            if len(lose) == 2:  # Binding precedes the operation; lose its checkpoint reply.
                raise QuorumUnavailable("checkpoint reply lost")
        return result

    monkeypatch.setattr(operator.cluster, "compare", compare)
    receipt = operator.run()
    assert receipt["state"] == "outcome_unconfirmed"
    assert operator.run()["result"]["attached"] is True


@pytest.mark.parametrize("failure", [CirraError, sqlite3.OperationalError])
def test_native_error_after_write_is_checkpointed_as_failure_not_refusal(
    operator, monkeypatch, failure
):
    original = coordinated_ops.audit_cli.run

    def error_after_write(args):
        original(args)
        raise failure("failure after a native write")

    monkeypatch.setattr(coordinated_ops.audit_cli, "run", error_after_write)
    receipt = operator.run()
    assert receipt["state"] == "failed" and receipt["authority_checkpointed"]
    assert receipt["exit_code"] == 1 and len(list(AuditQueue(operator.state).jobs())) == 1
    assert receipt["error_type"] == failure.__name__


def test_changed_policy_and_unlisted_command_refused_without_lease(operator):
    operator.config.routes.clear()
    with pytest.raises(CirraError, match="policy differs"):
        operator.run()
    with pytest.raises(CirraError, match="not available"):
        operator.run("serve")
    assert not operator.owners


def test_parser_only_exposes_allowlisted_commands():
    parser = coordinated_ops.parser()
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["--service-config", "service.yaml", "ingest-event"])
    assert error.value.code == 2


def test_malformed_dispatch_request_refused_without_lease(operator, tmp_path):
    request = tmp_path / "bad-request.json"
    request.write_text("[]")
    with pytest.raises(CirraError, match="repository has no matching enrolled route"):
        operator.run("dispatch-release", request=request, retry_uncertain=False)
    assert not operator.owners


def test_retry_publication_schedules_the_native_reconcile_path(operator):
    requested = operator.run(tier="pr", base="c" * 40, ref=None, change=7)
    queue = AuditQueue(operator.state, operator.store)
    job = queue.read(requested["result"]["job_id"])
    attempt, _ = audits.begin(operator.store, job["identity"], "change:7")
    with operator.store.transaction() as connection:
        connection.execute(
            "UPDATE audits SET state='complete' WHERE attempt_id=?", (attempt["attempt_id"],)
        )
    job.update(
        state="attention",
        attempt_id=attempt["attempt_id"],
        result={"publication": {"state": "failed"}},
    )
    queue.save(job)
    receipt = operator.run("retry-audit-publication", job=job["id"])
    assert receipt["state"] == "complete" and receipt["authority_checkpointed"]
    assert receipt["result"]["state"] == "awaiting_publication"
    assert audits.get(operator.store, attempt["attempt_id"])["state"] == "complete"


def test_success_receipt_corresponds_to_advanced_original_authority(operator):
    before = json.loads(operator.cluster.get(AUTHORITY))
    receipt = operator.run()
    after = json.loads(operator.cluster.get(AUTHORITY))
    assert receipt["authority_checkpointed"]
    assert before["host"] == after["host"] and before["epoch"] == after["epoch"]
    assert before["tip"] != after["tip"]


def test_plan_release_preserves_gate_and_checkpoints_queued_audit(
    operator, release_candidate, tmp_path
):
    _, _, _, review_store, report = release_candidate
    with review_store.connection() as source, operator.store.connection() as destination:
        source.backup(destination)
    path = tmp_path / "request.json"
    arguments = {"run": report["run_id"], "workflow_id": 12, "waive_audit": None, "output": path}
    refused = operator.run("plan-release", **arguments)
    assert refused["state"] == "failed" and refused["authority_checkpointed"]
    [job] = AuditQueue(operator.state).jobs()
    assert job["tier"] == "release" and not path.exists()
    store_release_audit(operator.store, operator.config.routes["repo"].policy.audit, report)
    planned = operator.run("plan-release", **arguments)
    assert planned["state"] == "complete" and planned["authority_checkpointed"]
    request = json.loads(path.read_text())
    assert request["audit_gate"]["state"] == "passed"
    assert request["request_id"] == planned["result"]["request_id"]
