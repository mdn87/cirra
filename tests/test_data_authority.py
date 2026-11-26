import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from cirra.data_authority import AUTHORITY, DAY, DataAuthority
from cirra.domain import CirraError, digest
from cirra.quorum import QuorumUnavailable


class Quorum:
    def __init__(self):
        self.values = {}
        self.token = "owner"
        self.lose_reply = False

    def get(self, key):
        return self.values.get(key)

    def compare(self, checks, puts):
        if any(self.values.get(key) != value for key, value in checks.items()):
            return False
        self.values.update(puts)
        if self.lose_reply and puts:
            self.lose_reply = False
            raise QuorumUnavailable("reply lost")
        return True


def data_host(cluster, root, name):
    root.mkdir(exist_ok=True)
    quorum = SimpleNamespace(
        get=cluster.get,
        config=SimpleNamespace(node=name, preferred_node="linux", standby_node="mac"),
    )
    return DataAuthority(
        SimpleNamespace(
            quorum=quorum, queue=SimpleNamespace(directory=root / "queue"), opaque=digest
        )
    )


def read(root, filename):
    return json.loads((root / filename).read_bytes())


def snapshot(source, destination, now, *, backup=False):
    marker = read(source, "data-authority.json")
    record = {
        **{key: marker[key] for key in ("host", "epoch", "tip")},
        "snapshot_id": digest([now]),
        "captured_at": datetime.fromtimestamp(now, UTC).isoformat(),
    }
    if backup:
        path = destination / "data-backup.json"
    else:
        (destination / "data-authority.json").write_text(json.dumps(marker))
        path = destination / "data-restored.json"
    path.write_text(json.dumps(record))
    return record


def test_verified_promotion_and_final_reverse_transfer(tmp_path, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr("cirra.data_authority._stamp", lambda: now)
    cluster = Quorum()
    primary = data_host(cluster, tmp_path / "primary", "linux")
    mac = data_host(cluster, tmp_path / "standby", "mac")
    primary.initialize(cluster)
    assert mac.expected_donor() == primary.host
    assert primary.expected_donor() is None
    snap = snapshot(primary.root, mac.root, now)
    primary.checkpoint(cluster)
    assert mac.eligible()
    assert mac.accepts_restore() and not primary.accepts_restore()
    now += DAY
    assert not mac.eligible()
    now -= 10
    mac.activate(cluster)
    assert mac.eligible() and not mac.accepts_restore() and not primary.eligible()
    assert primary.expected_donor() is None
    with pytest.raises(QuorumUnavailable):
        primary.activate(cluster)
    early = data_host(cluster, tmp_path / "early-linux", "linux")
    snapshot(mac.root, early.root, now)
    assert not early.eligible()
    now += 11
    assert not mac.eligible()
    # The donor's old backup cannot extend a new host's recovery deadline.
    (mac.root / "data-backup.json").write_text(json.dumps(snap))
    assert not mac.eligible()
    snapshot(mac.root, mac.root, now, backup=True)
    assert mac.eligible()
    mac.freeze(cluster, "linux")
    assert primary.expected_donor() == mac.host
    assert not mac.eligible() and not early.eligible()
    destination = data_host(cluster, tmp_path / "recovered-primary", "linux")
    snapshot(mac.root, destination.root, now)
    assert destination.eligible() and destination.accepts_restore()
    destination.activate(cluster)
    assert destination.eligible() and not mac.eligible() and not primary.eligible()
    assert "linux" not in cluster.values[AUTHORITY] and "mac" not in cluster.values[AUTHORITY]


def test_stale_same_host_state_and_freshness_cannot_rewind_the_private_tip(tmp_path):
    now = int(datetime.now(UTC).timestamp())
    cluster = Quorum()
    primary = data_host(cluster, tmp_path / "primary", "linux")
    rollback = data_host(cluster, tmp_path / "obsolete", "linux")
    primary.initialize(cluster)
    snapshot(primary.root, rollback.root, now)
    assert not rollback.eligible()  # Even if its snapshot is the latest registered tip.
    with pytest.raises(CirraError, match="current authoritative"):
        rollback.freeze(cluster, "mac")
    (rollback.root / "data-restored.json").rename(rollback.root / "preserved-receipt.json")
    primary.checkpoint(cluster)
    assert not rollback.eligible()  # Old raw copy predates the latest private completion.
    with pytest.raises(CirraError, match="differs"):
        rollback.initialize(cluster)
    mac = data_host(cluster, tmp_path / "standby", "mac")
    receipt = snapshot(primary.root, mac.root, now)
    (mac.root / "data-restored.json").write_text(json.dumps({**receipt, "tip": "a" * 64}))
    assert not mac.eligible()
    with pytest.raises(CirraError, match="other configured"):
        primary.freeze(cluster, "other")


def test_interrupted_promotion_and_freeze_recover_without_a_second_transition(tmp_path):
    now = int(datetime.now(UTC).timestamp())
    cluster = Quorum()
    primary = data_host(cluster, tmp_path / "primary", "linux")
    mac = data_host(cluster, tmp_path / "standby", "mac")
    cluster.lose_reply = True
    with pytest.raises(QuorumUnavailable, match="lost"):
        primary.initialize(cluster)
    primary.initialize(cluster)
    snapshot(primary.root, mac.root, now)
    cluster.lose_reply = True
    with pytest.raises(QuorumUnavailable, match="lost"):
        mac.activate(cluster)
    after = cluster.values[AUTHORITY]
    assert mac.eligible() and not primary.eligible()
    mac.activate(cluster)
    assert cluster.values[AUTHORITY] == after
    cluster.lose_reply = True
    with pytest.raises(QuorumUnavailable, match="lost"):
        mac.freeze(cluster, "linux")
    frozen = cluster.values[AUTHORITY]
    mac.freeze(cluster, "linux")
    assert not mac.eligible() and cluster.values[AUTHORITY] == frozen


def test_backup_only_recovery_checks_original_unfrozen_private_state_after_expiry(
    tmp_path, monkeypatch
):
    now = 1_800_000_000
    monkeypatch.setattr("cirra.data_authority._stamp", lambda: now)
    cluster = Quorum()
    primary = data_host(cluster, tmp_path / "primary", "linux")
    mac = data_host(cluster, tmp_path / "mac", "mac")
    rollback = data_host(cluster, tmp_path / "older-copy", "linux")
    assert not primary.can_recover_backup()
    primary.initialize(cluster)
    snapshot(primary.root, rollback.root, now)
    snapshot(primary.root, mac.root, now)
    assert not rollback.can_recover_backup() and not mac.can_recover_backup()
    # A committed private tip can be recovered after a crash during its local update.
    cluster.lose_reply = True
    with pytest.raises(QuorumUnavailable):
        primary.checkpoint(cluster)
    now += DAY + 1
    assert not primary.eligible() and primary.can_recover_backup()
    before = json.loads(cluster.get(AUTHORITY))
    cluster.token = "new-backup-owner"
    primary.activate_backup(cluster)
    active = json.loads(cluster.get(AUTHORITY))
    assert read(primary.root, "data-authority.json") == active
    assert {key for key in before if before[key] != active[key]} == {"session", "tip"}
    assert active["session"] == digest(["private-session", cluster.token])
    primary.activate_backup(cluster)
    assert json.loads(cluster.get(AUTHORITY)) == active
    assert not primary.eligible()
    (rollback.root / "data-restored.json").rename(rollback.root / "older-receipt.json")
    assert not rollback.can_recover_backup()
    snapshot(primary.root, primary.root, now, backup=True)
    assert primary.eligible()
    primary.freeze(cluster, "mac")
    assert not primary.can_recover_backup()
    with pytest.raises(QuorumUnavailable):
        primary.activate_backup(cluster)
    snapshot(primary.root, mac.root, now)
    assert mac.accepts_restore() and not mac.can_recover_backup()
    with pytest.raises(QuorumUnavailable):
        mac.activate_backup(cluster)
