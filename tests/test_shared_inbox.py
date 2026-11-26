"""Private state loss, inconsistent quorum history, and exhausted local capacity."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from cirra import coordination
from cirra.domain import CirraError, DeliveryConflict, digest
from cirra.health import check
from cirra.job_queue import Queue
from cirra.publish import write_receipt
from cirra.queue_format import activate
from cirra.quorum import OWNER_KEY, Owner, Quorum, QuorumConfig, QuorumUnavailable, bind_owner
from cirra.service_config import ServiceConfig
from cirra.shared_inbox import COUNT, PENDING, SharedInbox
from cirra.store import Store
from cirra.worker import heartbeat


class MemoryQuorum:
    def __init__(self):
        self.state = {}

    def get(self, key):
        return self.state.get(key)

    def compare(self, checks, puts, *, deletes=()):
        if any(self.get(key) != old for key, old in checks.items()):
            return False
        for key in deletes:
            self.state.pop(key, None)
        self.state.update(puts)
        return True

    def scan(self, prefix, after="", limit=32):
        rows = sorted((k, v) for k, v in self.state.items() if k.startswith(prefix) and k > after)
        return rows[:limit], len(rows) > limit


@pytest.fixture
def systems(tmp_path):
    route = SimpleNamespace(
        name="private-route",
        digest="private-policy",
        repository="example/secret",
        repository_id="42",
        provider="github",
        instance="https://github.com",
        reader_env="READER",
    )
    policy = SimpleNamespace(
        routing_key_env="ROUTING_KEY", node="first", preferred_node="first", standby_node="second"
    )
    config = ServiceConfig({route.name: route}, 1, 1, 3, 2, 10, coordination=policy)
    voters = MemoryQuorum()
    owner = Owner(voters)
    voters.state[OWNER_KEY] = owner.token
    origins = [
        SharedInbox(config, voters, Queue(tmp_path / node, 2), lambda _: "a" * 32)
        for node in ("a", "b")
    ]
    origins[0].initialize(owner)
    return *origins, voters, owner, route


def hint(text, change=73):
    return {"change": change, "delivery": text, "payload_digest": digest(["private body", text])}


def test_duplicate_conflict_and_ledger_evidence(systems):
    a, b, voters, owner, route = systems
    original = hint("private-delivery")
    accepted = a.enqueue(route, original)
    oid = accepted["job_id"]
    assert b.enqueue(route, {**original, "delivery": "other-private-delivery"}) == {
        "job_id": oid,
        "duplicate": True,
    }
    with pytest.raises(DeliveryConflict):
        b.enqueue(route, {**hint("another-payload"), "delivery": original["delivery"]})
    assert voters.get(COUNT) == "1"
    stored = json.dumps(voters.state)
    assert all(
        value not in stored
        for value in (
            "private-delivery",
            "example/secret",
            "private-route",
            "private-policy",
            "private body",
        )
    )
    with bind_owner(owner):
        b.materialize(owner)
        path = b.queue.directory / (oid + ".json")
        job = b.queue.review_record(path)
        assert b.require_pending(owner, job)
        changed = {**job, "hint": {**job["hint"], "change": 74}}
        with pytest.raises(QuorumUnavailable, match="differs"):
            b.require_pending(owner, changed)
        b.settle(owner, oid, "done")
        assert voters.get(COUNT) == "0" and voters.get(PENDING + oid) is None
        assert not b.require_pending(owner, job)
    assert a.enqueue(route, original)["duplicate"]
    voters.state.pop("inbox/settled/" + oid)
    with pytest.raises(QuorumUnavailable, match="original admission"):
        a.enqueue(route, original)
    # Rehydration is private execution, not a second HTTP submission.
    counts = json.loads(
        next((b.queue.directory.parent / "webhook-admissions").glob("*.json")).read_text()
    )
    assert (counts["accepted"], counts["duplicate"], counts["refused"]) == (0, 1, 0)


def test_full_private_queue_preserves_unmaterialized_shared_job(systems):
    a, b, voters, owner, route = systems
    b.queue.enqueue(route, {**hint("local-1"), "source": "reconcile"})
    b.queue.enqueue(route, {**hint("local-2"), "source": "reconcile"})
    original = hint("authenticated")
    oid = a.enqueue(route, original)["job_id"]
    b.remember(route, original["change"])
    with bind_owner(owner):
        b.materialize(owner)
        assert voters.get(PENDING + oid) and not (b.queue.directory / (oid + ".json")).exists()
        old = b.queue.scan()[0][0]
        old["state"] = "done"
        b.queue.save(old)
        b.materialize(owner)
        assert (b.queue.directory / (oid + ".json")).is_file()
    assert voters.get(COUNT) == "1"


def test_policy_and_cache_mismatches(systems):
    a, b, voters, owner, route = systems
    original = hint("one")
    oid = a.enqueue(route, original)["job_id"]
    identity = b.change_id(route, original["change"])
    write_receipt(b.cache / (identity + ".json"), {"change": 999}, history=False)
    assert b.private_change(route, identity) is None
    b.remember(route, original["change"])
    assert b.private_change(route, identity) == 73
    with bind_owner(owner):
        b.materialize(owner)
        path = b.queue.directory / (oid + ".json")
        job = b.queue.review_record(path)
        job["state"] = "done"
        job["hint"]["change"] = 88
        b.queue.save(job)
        with pytest.raises(QuorumUnavailable, match="private job differs"):
            b.materialize(owner)
    assert voters.get(COUNT) == "1"


def test_enrolled_local_queue_and_direct_cli_fail_closed(tmp_path):
    from cirra.cli import main

    root = tmp_path / "state"
    queue = Queue(root)
    activate(root, minimum=2)
    with pytest.raises(CirraError, match="committed shared job"):
        queue.enqueue(SimpleNamespace(name="r"), hint("bypass"))
    assert main(["show", "--state-dir", str(root), "--run", "x", "--export"]) == 2
    with pytest.raises(CirraError, match="quorum configuration"):
        coordination.connect(SimpleNamespace(coordination=None), root, None)
    write_receipt(root / "worker-queue-format.json", {"format": 1}, history=False)
    write_receipt(root / coordination.INTENT, {"cluster": "123", "namespace": "lab"}, history=False)
    assert coordination.shared_state(root)
    with pytest.raises(CirraError, match="quorum configuration"):
        coordination.connect(SimpleNamespace(coordination=None), root, None)


def test_legacy_uncertain_receipt_and_health_roles(tmp_path):
    root = tmp_path / "service"
    write_receipt(root / "audit-publication" / "old.json", {"state": "failed"}, history=False)
    with pytest.raises(CirraError, match="reconcile the existing publication"):
        coordination.check_legacy_work(root)
    write_receipt(
        root / "audit-publication" / "old.json",
        {"state": "failed", "uncertain": False},
        history=False,
    )
    coordination.check_legacy_work(root)
    q = Queue(root)
    for state in ("waiting_for_quorum", "waiting_for_private_state", "standby"):
        heartbeat(q, state=state)
        report = check(root)
        assert any(p["kind"] == "coordination" for p in report["problems"]) == (state != "standby")


def test_enrolled_saved_reports_open_only_read_access(tmp_path):
    import sqlite3

    Store(tmp_path)
    viewer = Store(tmp_path, readonly=True)
    with viewer.connection() as db:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM claims WHERE 0")


def test_pinned_config_and_bounded_reply(tmp_path):
    policy = {
        "endpoints": ["https://one:2379", "https://two:2379", "https://vote:2379"],
        "namespace": "cirra-test",
        "cluster_id": "123",
        "ca": "ca.pem",
        "certificate": "client.crt",
        "private_key": "client.key",
        "routing_key_env": "ROUTING_KEY",
        "node": "one",
        "preferred_node": "one",
        "standby_node": "two",
    }
    assert QuorumConfig.load(tmp_path, policy).cluster_id == "123"
    for override in (
        {"node": "vote"},
        {"standby_node": "one"},
        {"cluster_id": "0"},
        {"endpoints": ["http://one", "https://two", "https://vote"]},
        {"endpoints": ["https://one", "https://ONE:443", "https://vote"]},
        {"endpoints": ["https://one/path", "https://two", "https://vote"]},
    ):
        with pytest.raises(CirraError):
            QuorumConfig.load(tmp_path, {**policy, **override})
    quorum = object.__new__(Quorum)
    quorum.prefix = "/cirra/test/"
    quorum.config = replace(QuorumConfig.load(tmp_path, policy), cluster_id="124")

    class Response:
        status = 200

        def read(self, size):
            return b'{"header":null}'

    connection = SimpleNamespace(request=lambda *args: None, getresponse=Response)
    with pytest.raises(QuorumUnavailable, match="cluster identity"):
        quorum._post(connection, "kv/range", {})
    quorum.call = lambda *args: {"kvs": [{"key": "bad", "value": "bad"}]}
    with pytest.raises(QuorumUnavailable, match="single-key"):
        quorum.get("one")


def test_recovery_cache_checks_local_bytes_before_trusting_saved_id(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from cirra import private_transfer, standby_install

    cached = tmp_path / "cache" / "current.zip"
    donor, restic_id = "a" * 64, "b" * 64
    config = SimpleNamespace(coordination=SimpleNamespace(recovery_archive=cached))
    auth = SimpleNamespace(expected_donor=lambda: donor)
    monkeypatch.setattr(standby_install, "connect", lambda *args: object())
    monkeypatch.setattr(standby_install, "DataAuthority", lambda inbox: auth)
    monkeypatch.setattr(private_transfer, "configured_witness", lambda *args: b"x" * 32)
    observed = []

    def fetch(expected, archive, witness, *, known_snapshot):
        observed.append(known_snapshot)
        assert expected == donor and witness == b"x" * 32
        if known_snapshot == restic_id:
            return {"status": "unchanged", "restic_snapshot_id": restic_id}
        archive.write_bytes(b"verified signed portable archive")
        return {
            "status": "fetched",
            "host": donor,
            "captured_at": datetime.now(UTC).isoformat(),
            "restic_snapshot_id": restic_id,
        }

    monkeypatch.setattr(private_transfer, "fetch_latest", fetch)
    assert standby_install.refresh(config, tmp_path, None)["status"] == "fetched"
    assert standby_install.refresh(config, tmp_path, None)["status"] == "unchanged"
    cached.write_bytes(b"partial")
    assert standby_install.refresh(config, tmp_path, None)["status"] == "fetched"
    assert observed == [None, restic_id, None]

    def refuse(*args, **kwargs):
        raise CirraError("verification failed")

    monkeypatch.setattr(private_transfer, "fetch_latest", refuse)
    with pytest.raises(CirraError, match="verification failed"):
        standby_install.refresh(config, tmp_path, None)
    assert cached.read_bytes() == b"verified signed portable archive"


def test_private_installer_preserves_a_real_state_directory(tmp_path):
    from cirra.standby_install import install

    work = tmp_path / "current"
    work.mkdir()
    sentinel = work / "sentinel"
    sentinel.write_text("original")
    with pytest.raises(CirraError, match="managed state-directory symlink"):
        install(None, work, None, tmp_path / "absent")
    assert sentinel.read_text() == "original"
