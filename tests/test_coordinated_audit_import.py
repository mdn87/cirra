"""Operator imports obey native authority, immutable report and state-selection fences."""

import json
import runpy
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from audit_fixtures import Repository, audit_policy, build_report, write_report
from test_data_authority import Quorum

from cirra import audits
from cirra.data_authority import AUTHORITY, DataAuthority
from cirra.domain import CirraError, canonical, digest
from cirra.locking import change_lock
from cirra.quorum import (
    OWNER_KEY,
    Owner,
    OwnershipLost,
    QuorumUnavailable,
    bind_owner,
    current_owner,
)
from cirra.store import Store


@pytest.fixture
def world(tmp_path_factory, policy_data):
    # The managed restore version and attempt IDs are deliberately long. Avoid
    # adding the descriptive test name to Git for Windows' worktree path limit.
    tmp_path = tmp_path_factory.mktemp("cai")
    module = runpy.run_path(str(Path(__file__).parents[1] / "scripts/import-coordinated-audit.py"))
    operation = module["import_coordinated"]
    state = tmp_path / "current.restores" / ("a" * 64)
    state.mkdir(parents=True)
    selector = tmp_path / "current"
    try:
        selector.symlink_to(state.relative_to(tmp_path), target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    repo = Repository(tmp_path / "source")
    base = repo.commit({"app.py": "value = 1\n"})
    head = repo.commit({"app.py": "value = 2\n"})
    policy = audit_policy(tmp_path, policy_data)
    store = Store(state)
    identity = audits.audit_identity(
        policy.audit, "pr", audits.select_repository(policy), head, base
    )
    attempt, _ = audits.begin(store, identity, "change:1")
    checkout = state / "audit-attempts" / attempt["attempt_id"] / "checkout"
    checkout.parent.mkdir(parents=True)
    repo.git("worktree", "add", "--detach", str(checkout), head)
    report = build_report("pr", head, base, repo_path=checkout)
    path = write_report(tmp_path / "reports", report)
    cluster = Quorum()
    cluster.busy = False
    cluster.lost = False
    cluster.acquisitions = 0

    class Owner:
        token = "import-owner"

        def get(self, key):
            return cluster.get(key)

        def compare(self, checks, puts):
            if cluster.lost:
                raise OwnershipLost("lost lease")
            return cluster.compare(checks, puts)

        @contextmanager
        def maintaining(self):
            with bind_owner(self):
                yield self

    owner = Owner()

    def acquire():
        cluster.acquisitions += 1
        return None if cluster.busy else owner

    config = SimpleNamespace(node="linux", preferred_node="linux", standby_node="mac")
    inbox = SimpleNamespace(
        quorum=SimpleNamespace(get=cluster.get, config=config, acquire=acquire),
        queue=SimpleNamespace(directory=state / "queue"),
        opaque=digest,
        check=lambda: None,
    )
    authority = DataAuthority(inbox)
    authority.initialize(cluster)
    operation.__globals__["connect"] = lambda *args: inbox
    args = (
        SimpleNamespace(),
        selector,
        attempt["attempt_id"],
        path,
        digest(report),
        lambda name: None,
    )
    return SimpleNamespace(
        operation=operation,
        args=args,
        cluster=cluster,
        owner=owner,
        state=state,
        selector=selector,
        store=store,
        attempt=attempt,
        path=path,
        report=report,
        config=config,
        authority=authority,
    )


def test_import_and_replay_use_native_lease_without_another_attempt(world, monkeypatch):
    original = audits.import_report
    observed = []

    def guarded(*args):
        observed.append(current_owner())
        return original(*args)

    monkeypatch.setattr(audits, "import_report", guarded)
    before = json.loads(world.cluster.get(AUTHORITY))
    result = world.operation(*world.args)
    assert result["state"] == "complete" and result["authority_checkpointed"]
    assert not result["reused"]
    assert world.operation(*world.args)["reused"]
    after = json.loads(world.cluster.get(AUTHORITY))
    assert before["epoch"] == after["epoch"] and before["tip"] != after["tip"]
    assert observed == [world.owner, world.owner] and current_owner() is None
    assert audits.get(world.store, world.attempt["attempt_id"])["number"] == 1


def test_a_changed_report_is_refused_before_lease_acquisition(world):
    world.path.write_text(json.dumps({**world.report, "notes": ["changed after review"]}))
    with pytest.raises(CirraError, match="reviewed digest"):
        world.operation(*world.args)
    assert world.cluster.acquisitions == 0
    assert audits.get(world.store, world.attempt["attempt_id"])["state"] == "prepared"


def test_standby_cannot_promote_itself_for_an_import(world):
    world.config.node = "mac"
    with pytest.raises(QuorumUnavailable, match="original unfrozen"):
        world.operation(*world.args)
    assert world.cluster.acquisitions == 0
    assert audits.get(world.store, world.attempt["attempt_id"])["state"] == "prepared"


def test_existing_worker_lease_refuses_import(world):
    world.cluster.busy = True
    with pytest.raises(QuorumUnavailable, match="publishing lease"):
        world.operation(*world.args)
    assert audits.get(world.store, world.attempt["attempt_id"])["state"] == "prepared"


def test_local_worker_lock_refuses_import_before_acquiring_a_lease(world):
    with change_lock(world.state / "queue" / "locks", "worker", 0):
        with pytest.raises(CirraError):
            world.operation(*world.args)
    assert world.cluster.acquisitions == 0
    assert audits.get(world.store, world.attempt["attempt_id"])["state"] == "prepared"


def test_frozen_authority_cannot_be_reopened_for_import(world):
    world.authority.freeze(world.cluster, "mac")
    with pytest.raises(QuorumUnavailable, match="original unfrozen"):
        world.operation(*world.args)
    assert world.cluster.acquisitions == 0


def test_authority_changed_during_acquisition_refuses_import(world):
    inbox = world.operation.__globals__["connect"]()

    def freeze_before_acquisition():
        world.authority.freeze(world.cluster, "mac")
        return world.owner

    inbox.quorum.acquire = freeze_before_acquisition
    with pytest.raises(QuorumUnavailable, match="changed during import admission"):
        world.operation(*world.args)
    assert audits.get(world.store, world.attempt["attempt_id"])["state"] == "prepared"


def test_lost_lease_reports_committed_but_unconfirmed(world, monkeypatch):
    original = audits.import_report

    def lose_after_store(*args):
        result = original(*args)
        world.cluster.lost = True
        return result

    monkeypatch.setattr(audits, "import_report", lose_after_store)
    with pytest.raises(CirraError, match="committed but unconfirmed") as error:
        world.operation(*world.args)
    assert error.value.receipt["state"] == "committed_unconfirmed"
    assert error.value.receipt["authority_checkpointed"] is False
    assert audits.get(world.store, world.attempt["attempt_id"])["state"] == "complete"
    assert current_owner() is None


def test_editing_original_during_import_cannot_replace_accepted_json(world, monkeypatch):
    original = audits.import_report

    def replace_original(*args):
        world.path.write_text(json.dumps({**world.report, "notes": ["late replacement"]}))
        return original(*args)

    monkeypatch.setattr(audits, "import_report", replace_original)
    result = world.operation(*world.args)
    assert result["report_digest"] == digest(world.report)


def test_oversized_markdown_refused_before_lease_or_import(world):
    world.path.with_suffix(".md").write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    with pytest.raises(CirraError, match="Markdown"):
        world.operation(*world.args)
    assert world.cluster.acquisitions == 0
    assert audits.get(world.store, world.attempt["attempt_id"])["state"] == "prepared"


def test_symlinked_markdown_refused_before_lease(world):
    path = world.path.with_suffix(".md")
    original = path.with_name("retained-report.md")
    path.rename(original)
    path.symlink_to(original)
    with pytest.raises(CirraError, match="Markdown"):
        world.operation(*world.args)
    assert world.cluster.acquisitions == 0


@pytest.fixture
def real_owner(world):
    class TestQuorum:
        config = SimpleNamespace(lease_seconds=3600)

        def get(self, key):
            return world.cluster.get(key)

        def compare(self, checks, puts, *, deletes=(), leases=None):
            applied = world.cluster.compare(checks, puts)
            if applied:
                for key in deletes:
                    world.cluster.values.pop(key, None)
            return applied

    quorum = TestQuorum()
    owners = []

    def acquire():
        owner = Owner(quorum)
        world.cluster.values[OWNER_KEY] = owner.token
        owners.append(owner)
        return owner

    world.operation.__globals__["connect"]().quorum.acquire = acquire
    return owners


def test_real_owner_expiry_after_sqlite_commit_has_safe_replay(world, real_owner, monkeypatch):
    original = audits.import_report
    checkpoint_tip = []

    def expire_after_commit(*args):
        result = original(*args)
        checkpoint_tip.append(world.cluster.get(AUTHORITY))
        real_owner[-1].deadline = 0
        return result

    monkeypatch.setattr(audits, "import_report", expire_after_commit)
    with pytest.raises(CirraError, match="committed but unconfirmed"):
        world.operation(*world.args)
    assert audits.get(world.store, world.attempt["attempt_id"])["state"] == "complete"
    assert world.cluster.get(AUTHORITY) == checkpoint_tip[0]
    monkeypatch.setattr(audits, "import_report", original)
    assert world.operation(*world.args)["reused"]
    assert world.cluster.get(AUTHORITY) != checkpoint_tip[0]


def test_checkpoint_lost_reply_converges_via_native_intent(world, real_owner, monkeypatch):
    original = audits.import_report

    def lose_checkpoint_reply(*args):
        result = original(*args)
        world.cluster.lose_reply = True
        return result

    monkeypatch.setattr(audits, "import_report", lose_checkpoint_reply)
    with pytest.raises(CirraError, match="committed but unconfirmed"):
        world.operation(*world.args)
    assert audits.get(world.store, world.attempt["attempt_id"])["state"] == "complete"
    assert (world.state / "data-authority-intent.json").is_file()
    monkeypatch.setattr(audits, "import_report", original)
    result = world.operation(*world.args)
    assert result["reused"] and result["authority_checkpointed"]


def test_authority_moved_after_commit_refuses_old_host_replay(world, real_owner, monkeypatch):
    original = audits.import_report

    def move_after_commit(*args):
        result = original(*args)
        authority = json.loads(world.cluster.values[AUTHORITY])
        authority["host"] = world.authority.secondary
        world.cluster.values[AUTHORITY] = canonical(authority)
        return result

    monkeypatch.setattr(audits, "import_report", move_after_commit)
    with pytest.raises(CirraError, match="committed but unconfirmed"):
        world.operation(*world.args)
    monkeypatch.setattr(audits, "import_report", original)
    with pytest.raises(CirraError, match="original unfrozen"):
        world.operation(*world.args)


def test_schema_mismatch_is_refused_without_migration_or_lease(world):
    with world.store.connection() as connection:
        connection.execute("PRAGMA user_version=2")
    with pytest.raises(CirraError, match="compatible read-only schema"):
        world.operation(*world.args)
    assert world.cluster.acquisitions == 0
    with world.store.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_readback_database_error_reports_committed_unconfirmed(world, monkeypatch):
    original = audits.import_report

    def readback_error(*args):
        raise sqlite3.OperationalError("private database readback failed")

    def fail_readback_after_commit(*args):
        result = original(*args)
        monkeypatch.setattr(audits, "get", readback_error)
        return result

    monkeypatch.setattr(audits, "import_report", fail_readback_after_commit)
    with pytest.raises(CirraError, match="committed but unconfirmed") as error:
        world.operation(*world.args)
    assert error.value.receipt["state"] == "committed_unconfirmed"
    assert "private database" not in json.dumps(error.value.receipt)
