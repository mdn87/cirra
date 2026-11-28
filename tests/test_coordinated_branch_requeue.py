"""Operator recovery retains ownership, exact-job and quota fences."""

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_coordinated_audit_import import world  # noqa: F401

from cirra.data_authority import AUTHORITY
from cirra.domain import CirraError, digest
from cirra.job_queue import Queue
from cirra.quorum import OwnershipLost, QuorumUnavailable


@pytest.fixture
def recovery(world):  # noqa: F811
    operation = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts/requeue-coordinated-branch.py")
    )["requeue_branch"]
    operation.__globals__["connect"] = world.operation.__globals__["connect"]
    queue = Queue(world.args[1])
    route = SimpleNamespace(name="repo", digest="policy", branches=("main",), observer="observer")
    identifier = queue.enqueue(
        route,
        {
            "source": "branch",
            "branch": "main",
            "change": "branch:main",
            "delivery": "branch",
            "payload_digest": "branch",
        },
    )["job_id"]
    job = queue.review_record(queue.directory / (identifier + ".json"))
    job.update(state="attention", budget_sequence=7)
    queue.save(job)
    config = SimpleNamespace(routes={"repo": route}, max_jobs=10000)
    args = (
        config,
        world.args[1],
        identifier,
        digest(job),
        "Verified prior execution; refresh current head",
        lambda _: None,
    )
    return world, operation, args, queue


def test_requeue_preserves_charge_sequence_history_and_authority(recovery):
    fixture, operation, args, queue = recovery
    before = json.loads(fixture.cluster.get(AUTHORITY))
    result = operation(*args)
    after = json.loads(fixture.cluster.get(AUTHORITY))
    assert result["state"] == "queued" and result["authority_checkpointed"]
    job = queue.review_record(queue.directory / (args[2] + ".json"))
    assert job["budget_sequence"] == 7 and job["attempts"] == 0
    assert before["epoch"] == after["epoch"] and before["tip"] != after["tip"]
    assert list((queue.directory / "history").glob(args[2] + "-*.json"))


@pytest.mark.parametrize("scenario", ["busy", "lost", "changed", "model_review"])
def test_requeue_refuses_without_exact_job_and_current_authority(recovery, scenario):
    fixture, operation, args, queue = recovery
    if scenario in ("busy", "lost"):
        setattr(fixture.cluster, scenario, True)
    elif scenario == "changed":
        args = (*args[:3], "0" * 64, *args[4:])
    else:
        job = queue.review_record(queue.directory / (args[2] + ".json"))
        job["hint"]["source"] = "manual"
        queue.save(job)
        args = (*args[:3], digest(job), *args[4:])
    with pytest.raises((CirraError, OwnershipLost, QuorumUnavailable)):
        operation(*args)
    assert queue.review_record(queue.directory / (args[2] + ".json"))["state"] == "attention"
