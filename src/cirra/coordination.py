"""Enrollment and fail-closed startup for a quorum-backed state directory."""

import hmac
import json
from datetime import UTC, datetime
from pathlib import Path

from cirra.domain import CirraError, canonical
from cirra.job_queue import Queue
from cirra.publish import write_receipt
from cirra.queue_format import activate, live_format
from cirra.quorum import Quorum
from cirra.shared_inbox import SharedInbox

MARKER = "coordination-state.json"
INTENT = "coordination-enrollment.json"
TRANSFER_KEY = "data/transfer-key"


def verify_transfer_key(client, policy, key, *, initialize=False):
    context = canonical(["cirra.private-transfer", policy.cluster_id, policy.namespace]).encode()
    proof = hmac.digest(key, context, "sha256").hex()
    if initialize and client.compare({TRANSFER_KEY: None}, {TRANSFER_KEY: proof}):
        return
    if client.get(TRANSFER_KEY) != proof:
        raise CirraError("the private recovery credential differs from the enrolled primary")


def shared_state(directory):
    root = Path(directory)
    return (root / MARKER).exists() or (root / INTENT).exists() or live_format(root) >= 2


def connect(config, directory, credential):
    policy = getattr(config, "coordination", None)
    root = Path(directory)
    if policy is None:
        if shared_state(root):
            raise CirraError("this enrolled state requires its quorum configuration")
        return None
    try:
        marker = json.loads((root / MARKER).read_text(encoding="utf8"))
    except (FileNotFoundError, ValueError) as exc:
        raise CirraError("enroll this data host before starting coordinated services") from exc
    if (
        marker != {"cluster": policy.cluster_id, "namespace": policy.namespace}
        or live_format(root) != 2
    ):
        raise CirraError("coordination configuration differs from this enrolled state")
    quorum = Quorum(policy)
    return SharedInbox(config, quorum, Queue(directory, config.max_jobs), credential)


def check_legacy_work(root):
    """An old process may already have issued a write. Empty quorum slots cannot disprove it."""
    unsafe = {
        "publication": {"uncertain"},
        "review-gates": {"awaiting_ack", "starting"},
        "observer": {"publishing"},
        "audit-publication": {"creating"},
        "releases": {"uncertain"},
    }
    for directory, states in unsafe.items():
        for path in (root / directory).glob("*.json"):
            row = json.loads(path.read_bytes())
            uncertain_audit = directory == "audit-publication" and (
                row.get("uncertain") is True
                or (row.get("state") == "failed" and row.get("uncertain") is not False)
            )
            if row.get("state") in states or uncertain_audit:
                raise CirraError(
                    f"reconcile the existing publication receipt before enrollment: {path.name}"
                )
    for directory, states in (
        ("queue", {"queued", "running", "retry"}),
        ("audit-queue", {"running", "awaiting_import", "awaiting_publication"}),
    ):
        for path in (root / directory).glob("*.json"):
            row = json.loads(path.read_bytes())
            if row.get("state") in states:
                raise CirraError(
                    f"reconcile unfinished {directory} work before enrollment: {path.name}"
                )


def enroll(config, directory, credential, *, initialize):
    from cirra.data_authority import DataAuthority
    from cirra.private_transfer import configured_witness
    from cirra.quorum_budget import enrollment_locks, seed_budgets

    policy = getattr(config, "coordination", None)
    if not policy:
        raise CirraError("enrollment requires an explicit coordination configuration")
    root = Path(directory).resolve()
    if not root.is_dir():
        raise CirraError("the primary or restored standby state directory must already exist")
    witness = configured_witness(config, credential)
    marker = root / MARKER
    desired = {"cluster": policy.cluster_id, "namespace": policy.namespace}
    for anchor in (root / INTENT, marker):
        if anchor.exists() and json.loads(anchor.read_text(encoding="utf8")) != desired:
            raise CirraError("refusing to rebind an enrolled directory to another cluster")
    quorum = Quorum(policy)
    inbox = SharedInbox(config, quorum, Queue(root, config.max_jobs), credential)
    usage = None
    if marker.exists():
        if live_format(root) != 2:
            raise CirraError("completed coordination enrollment requires queue format 2")
        inbox.check()
        verify_transfer_key(quorum, policy, witness)
        return {"enrolled": policy.node, "namespace": policy.namespace, "daily_usage": usage}

    def preflight():
        check_legacy_work(root)
        write_receipt(root / INTENT, desired, history=False)
        activate(root, minimum=2)

    def finish():
        write_receipt(marker, desired, history=False)

    if initialize:
        if policy.node != policy.preferred_node:
            raise CirraError("only the designated primary can initialize shared state")
        owner = quorum.acquire()
        if owner is None:
            raise CirraError("a current worker already owns this cluster")
        audits = [
            r.policy.audit.max_audits_per_day
            for r in config.routes.values()
            if getattr(r.policy, "audit", None) is not None
        ]
        with owner.maintaining():

            def finish_primary():
                verify_transfer_key(owner, policy, witness, initialize=True)
                DataAuthority(inbox).initialize(owner)
                inbox.initialize(owner)
                finish()

            usage = seed_budgets(
                owner,
                root,
                datetime.now(UTC).date(),
                config.max_jobs_per_day,
                min(audits, default=1),
                before_seed=preflight,
                after_seed=finish_primary,
            )
    else:
        with enrollment_locks(root):
            inbox.check()
            verify_transfer_key(quorum, policy, witness)
            preflight()
            finish()
    return {"enrolled": policy.node, "namespace": policy.namespace, "daily_usage": usage}


def coordination_control(config, directory, credential, target=None):
    from cirra.data_authority import DataAuthority
    from cirra.quorum import OWNER_KEY
    from cirra.quorum_budget import enrollment_locks
    from cirra.shared_inbox import COUNT

    inbox = connect(config, directory, credential)
    if inbox is None:
        raise CirraError("this command requires an enrolled service")
    inbox.check()
    authority, policy = DataAuthority(inbox), config.coordination
    if target is None:
        count = inbox.quorum.get(COUNT)
        if count is None or not count.isdecimal():
            raise CirraError("invalid shared intake counter")
        return {
            "node": policy.node,
            "local_data_host": authority.host,
            "namespace": policy.namespace,
            "pending_deliveries": int(count),
            "publishing_lease_held": inbox.quorum.get(OWNER_KEY) is not None,
            "private_state_eligible": authority.eligible(),
            "expected_donor": authority.expected_donor(),
        }
    if target not in {policy.preferred_node, policy.standby_node} - {policy.node}:
        raise CirraError("the transfer target must be the other enrolled data host")
    with enrollment_locks(Path(directory)):
        owner = inbox.quorum.acquire()
        if owner is None:
            raise CirraError("stop the source worker and confirm the publishing lease has released")
        with owner.maintaining():
            authority.freeze(owner, target)
    return {"frozen": policy.node, "transfer_to": target}
