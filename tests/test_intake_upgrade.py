"""Observer-only policy upgrades preserve admission without relaxing other policy pins."""

import copy
from types import SimpleNamespace

import pytest
import yaml
from test_shared_inbox import MemoryQuorum

from cirra.domain import CirraError
from cirra.job_queue import Queue
from cirra.quorum import OWNER_KEY, Owner, bind_owner
from cirra.service_config import load_service
from cirra.shared_inbox import SharedInbox


@pytest.fixture
def policy_pair(tmp_path, policy_data):
    policy_data["repositories"] = [
        {"provider": "github", "instance": "https://github.com", "repository_id": "42"}
    ]
    policy_data["reviewer"]["kind"] = "claude"
    policy_data["reviewer"]["endpoint"] = "cli://claude"
    policy_data["ci"] = {
        "mode": "required",
        "expected": [
            {
                "kind": "check_run",
                "source_id": "5",
                "name": "cirra/isolated-tests",
                "definition_digest": "a" * 64,
            }
        ],
    }
    raw = yaml.safe_dump(policy_data)
    (tmp_path / "policy.yaml").write_text(raw)
    (tmp_path / "intake-policy.yaml").write_text(raw)
    service = {
        "version": 1,
        "routes": [
            {
                "name": "test",
                "provider": "github",
                "repository": "example/repo",
                "repository_id": policy_data["repositories"][0]["repository_id"],
                "policy": "policy.yaml",
                "webhook_secret_env": "HOOK",
                "observer": "observer.yaml",
                "witness_key_env": "WITNESS",
                "publisher": {"app_id": 5, "installation_id": 6, "private_key_env": "KEY"},
            }
        ],
    }
    path = tmp_path / "service.yaml"
    path.write_text(yaml.safe_dump(service))
    original = load_service(path)
    updated = copy.deepcopy(policy_data)
    updated["ci"]["expected"][0]["definition_digest"] = "b" * 64
    (tmp_path / "policy.yaml").write_text(yaml.safe_dump(updated))
    service["routes"][0]["intake_policy"] = "intake-policy.yaml"
    path.write_text(yaml.safe_dump(service))
    return path, original, updated


def test_old_and_new_origins_share_admission_and_replay_identity(policy_pair, tmp_path):
    path, old, _ = policy_pair
    new = load_service(path)
    previous, current = old.routes["test"], new.routes["test"]
    assert current.digest != previous.digest
    assert current.intake_digest == previous.digest
    voters = MemoryQuorum()
    owner = Owner(voters)
    voters.state[OWNER_KEY] = owner.token
    policy = SimpleNamespace(routing_key_env="ROUTING", preferred_node="one", standby_node="two")
    from dataclasses import replace

    a = SharedInbox(
        replace(old, coordination=policy), voters, Queue(tmp_path / "old"), lambda _: "s" * 32
    )
    b = SharedInbox(
        replace(new, coordination=policy), voters, Queue(tmp_path / "new"), lambda _: "s" * 32
    )
    a.initialize(owner)
    assert a.policy == b.policy and a.route_id(previous) == b.route_id(current)
    hint = {"change": 7, "delivery": "delivery", "payload_digest": "c" * 64}
    accepted = a.enqueue(previous, hint)
    assert b.enqueue(current, hint)["duplicate"]
    b.remember(current, 7)
    with bind_owner(owner):
        b.materialize(owner)
    assert b.queue.scan()[0][0]["id"] == accepted["job_id"]


@pytest.mark.parametrize("change", ["model", "limit", "repository", "source"])
def test_intake_compatibility_refuses_unrelated_policy_changes(policy_pair, change):
    path, _, updated = policy_pair
    if change == "model":
        updated["reviewer"]["model"] = "a-different-model"
    elif change == "limit":
        updated["limits"]["max_diff_bytes"] += 1
    elif change == "repository":
        updated["repositories"][0]["repository_id"] = "987"
    else:
        updated["ci"]["expected"][0]["source_id"] = "66"
    (path.parent / "policy.yaml").write_text(yaml.safe_dump(updated))
    with pytest.raises(CirraError):
        load_service(path)


def test_upgrade_plan_retains_original_identity_and_is_idempotent(policy_pair, monkeypatch):
    from cirra import pins

    path, original, _ = policy_pair
    document = yaml.safe_load(path.read_bytes())
    del document["routes"][0]["intake_policy"]
    path.write_text(yaml.safe_dump(document))
    baseline = (path.parent / "intake-policy.yaml").read_bytes()
    (path.parent / "policy.yaml").write_bytes(baseline)
    monkeypatch.setattr(pins, "load_observer", lambda _: {"definition_digest": "c" * 64})
    before = path.read_bytes()
    updates = pins.upgrade_files(path)
    assert path.read_bytes() == before  # Preview has no configuration side effects.
    assert updates[path.parent / "intake-test.yaml"] == baseline
    for target, raw in updates.items():
        target.write_bytes(raw)
    upgraded = load_service(path)
    assert upgraded.routes["test"].intake_digest == original.routes["test"].digest
    assert pins.check_pins(path)[0]["ok"]
    assert pins.upgrade_files(path) == {}
    monkeypatch.setattr(pins, "load_observer", lambda _: {"definition_digest": "d" * 64})
    for target, raw in pins.upgrade_files(path).items():
        target.write_bytes(raw)
    assert (path.parent / "intake-test.yaml").read_bytes() == baseline
    assert load_service(path).routes["test"].intake_digest == original.routes["test"].digest
