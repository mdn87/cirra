import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cirra.coordinator import analysis_identity, review
from cirra.domain import digest
from cirra.store import LostClaim, Store

WORKER = Path(__file__).with_name("process_worker.py")


def launch(mode, tmp_path, rereview=False):
    args = [
        sys.executable,
        str(WORKER),
        mode,
        str(tmp_path / "policy.yaml"),
        str(tmp_path / "change.json"),
        str(tmp_path / "state"),
    ]
    if rereview:
        args.append("--rereview")
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def result(process):
    try:
        stdout, stderr = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    assert process.returncode == 0, stderr
    return json.loads(stdout)


def call_count(tmp_path):
    path = tmp_path / "state/calls"
    return len(path.read_bytes().splitlines()) if path.exists() else 0


def prepare(inputs, policy_data):
    # Shared runners can spend longer than a subsecond lease in SQLite setup.
    # Keep real processes/heartbeats, with enough time for ordinary scheduling.
    policy_data["limits"].update(claim_ttl_seconds=5, wait_seconds=20)
    return inputs(policy=policy_data)


def test_two_concurrent_processes_share_one_analysis(inputs, policy_data, tmp_path):
    prepare(inputs, policy_data)
    first = launch("normal", tmp_path)
    second = launch("normal", tmp_path)
    try:
        results = [result(first), result(second)]
    finally:
        for process in (first, second):
            if process.poll() is None:
                process.kill()
                process.communicate()
    assert call_count(tmp_path) == 1
    assert results[0]["report"]["run_id"] == results[1]["report"]["run_id"]
    assert sorted(item["reused"] for item in results) == [False, True]


@pytest.mark.parametrize("stage", ["before-call", "after-call", "after-response", "after-analysis"])
def test_killed_owner_recovers_without_repeating_an_uncertain_call(
    stage, inputs, policy_data, tmp_path
):
    prepare(inputs, policy_data)
    process = launch(stage, tmp_path)
    deadline = time.monotonic() + 60
    try:
        while not (tmp_path / "state/paused").exists():
            if process.poll() is not None:
                pytest.fail(process.communicate()[1])
            assert time.monotonic() < deadline, "worker did not reach its durable crash point"
            time.sleep(0.03)
    finally:
        process.kill()
        process.communicate(timeout=5)
    # The next process waits for lease expiry, then takes over transactionally.
    recovered = result(launch("normal", tmp_path))
    assert call_count(tmp_path) == 1
    report = recovered["report"]
    if stage == "after-call":
        assert report["review"]["outcome"] == "uncertain"
        assert report["status"] == "incomplete"
        repeated = result(launch("normal", tmp_path))
        assert repeated["report"]["review"]["outcome"] == "uncertain"
        assert call_count(tmp_path) == 1
        explicit = result(launch("normal", tmp_path, rereview=True))
        assert explicit["report"]["review"]["outcome"] == "succeeded"
        assert explicit["report"]["review"]["attempt"] == 2
        assert explicit["report"]["run_id"] != report["run_id"]
        assert call_count(tmp_path) == 2
    else:
        assert report["review"]["outcome"] == "succeeded"
        assert report["status"] == "complete"


def test_expired_and_superseded_owners_cannot_write(inputs, tmp_path, monkeypatch):
    provider, config = inputs()
    store = Store(tmp_path / "state")
    now = 1_000.0
    monkeypatch.setattr("cirra.store.time", SimpleNamespace(time=lambda: now))
    old = store.acquire(provider.snapshot.change_key, 10)
    identity = analysis_identity(provider, config)
    attempt, _ = store.attempt(old, digest(identity), identity)
    store.begin_call(old, attempt["run_id"])
    now += 10  # Exactly at expiry, independent of filesystem/scheduler speed.
    with pytest.raises(LostClaim):
        store.record_response(old, attempt["run_id"], {"raw": provider.response}, 0.1)
    current = store.acquire(provider.snapshot.change_key, 5)
    assert current.generation > old.generation
    with pytest.raises(LostClaim):
        store.record_response(old, attempt["run_id"], {"raw": provider.response}, 0.1)
    with pytest.raises(LostClaim):
        store.save_report(old, {})
    store.release(old)
    assert store.acquire(provider.snapshot.change_key, 5) is None
    store.heartbeat(current, 5)
    store.release(current)


def test_other_change_claim_cannot_mutate_attempt(inputs, tmp_path):
    provider, config = inputs()
    store = Store(tmp_path / "state")
    first = store.acquire(provider.snapshot.change_key, 5)
    identity = analysis_identity(provider, config)
    attempt, _ = store.attempt(first, digest(identity), identity)
    other = store.acquire("other-change", 5)
    from cirra.domain import CirraError

    with pytest.raises(CirraError, match="transition"):
        store.begin_call(other, attempt["run_id"])
    store.release(first)
    store.release(other)


@pytest.mark.parametrize("superseded", [False, True])
def test_expiry_after_analysis_retries_only_the_final_report(
    superseded, inputs, tmp_path, monkeypatch
):
    provider, config = inputs()
    store = Store(tmp_path / "state")
    generations = []
    calls = []
    save_report = store.save_report

    def expire_before_first_save(claim, report):
        if not generations:
            with store.transaction() as conn:
                conn.execute("UPDATE claims SET expires_at=0 WHERE owner=?", (claim.owner,))
            if superseded:
                other = store.acquire(claim.change_key, 20)
                store.release(other)
        generations.append(claim.generation)
        save_report(claim, report)

    def counted_review(packet):
        calls.append(packet)
        return provider.response

    monkeypatch.setattr(store, "save_report", expire_before_first_save)
    reviewer = SimpleNamespace(review=counted_review)
    if superseded:
        with pytest.raises(LostClaim):
            review(provider, config, store, reviewer=reviewer)
        assert len(generations) == 1
        with store.connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0
    else:
        report, reused = review(provider, config, store, reviewer=reviewer, rereview=True)
        assert generations == [1, 2]
        assert not reused and report["review"]["attempt"] == 1
        assert store.latest_report(report["run_id"]) == report
    assert len(calls) == 1
