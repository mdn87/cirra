"""Tie a publisher lease to recoverable, current private state on a data host."""

import json
import re
import uuid
from datetime import UTC, datetime

from cirra.domain import CirraError, canonical, digest
from cirra.publish import write_receipt
from cirra.quorum import QuorumUnavailable

AUTHORITY = "data/authority"
DAY = 24 * 60 * 60
HEX = re.compile(r"[0-9a-f]{64}")


def _read(path):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return None


def _stamp():
    return int(datetime.now(UTC).timestamp())


def _fresh(timestamp, now):
    return type(timestamp) is int and -60 <= now - timestamp < DAY


def valid_authority(record):
    return (
        isinstance(record, dict)
        and set(record) == {"host", "epoch", "tip", "session", "floor", "target"}
        and all(
            isinstance(record[key], str) and HEX.fullmatch(record[key])
            for key in ("host", "epoch", "tip", "session")
        )
        and type(record["floor"]) is int
        and (
            record["target"] is None
            or (isinstance(record["target"], str) and HEX.fullmatch(record["target"]) is not None)
        )
    )


class DataAuthority:
    def __init__(self, inbox):
        self.quorum = inbox.quorum
        self.root = inbox.queue.directory.parent
        self.config = inbox.quorum.config
        self.host = inbox.opaque(["data-host", self.config.node])
        self.primary = inbox.opaque(["data-host", self.config.preferred_node])
        self.secondary = inbox.opaque(["data-host", self.config.standby_node])

    def _current(self, client):
        raw = client.get(AUTHORITY)
        if raw is None:
            return None
        try:
            record = json.loads(raw)
        except (ValueError, TypeError):
            raise CirraError("invalid data-host authority") from None
        if (
            not valid_authority(record)
            or raw != canonical(record)
            or record["host"] not in (self.primary, self.secondary)
            or record["target"] not in (None, self.primary, self.secondary)
        ):
            raise CirraError("invalid data-host authority")
        return record

    def _local(self, current):
        local = _read(self.root / "data-authority.json")
        intent = _read(self.root / "data-authority-intent.json")
        # A process can lose the successful CAS reply or crash before writing
        # the local copy. A prepared transition is evidence only if quorum committed it.
        if (
            current is not None
            and isinstance(intent, dict)
            and current == intent.get("after")
            and local == intent.get("before_local")
        ):
            return current
        return local

    def _receipt_time(self, filename, expected, now, *, exact):
        receipt = _read(self.root / filename)
        if not expected or not isinstance(receipt, dict):
            return None
        if not all(
            isinstance(receipt.get(key), str) and HEX.fullmatch(receipt[key])
            for key in ("snapshot_id", "epoch", "tip", "host")
        ):
            return None
        keys = ("host", "epoch", "tip") if exact else ("host", "epoch")
        if any(receipt[key] != expected[key] for key in keys):
            return None
        try:
            captured = datetime.fromisoformat(receipt["captured_at"])
            if captured.utcoffset() is None:
                return None
            stamp = int(captured.timestamp())
        except (KeyError, ValueError, TypeError, OverflowError):
            return None
        return stamp if _fresh(stamp, now) else None

    def _restored_own_tenure(self, current):
        restored = _read(self.root / "data-restored.json")
        return isinstance(restored, dict) and restored.get("epoch") == current["epoch"]

    def _candidate(self, current):
        if not current:
            return False
        local = self._local(current)
        if not valid_authority(local):
            return False
        now = _stamp()
        if current["target"] is None and current["host"] == self.host:
            # A restored copy of this host's own tenure cannot substitute for
            # the original state, even if it was captured in the middle of a pass.
            if self._restored_own_tenure(current):
                return False
            return local == current and (
                _fresh(current["floor"], now)
                or self._receipt_time("data-backup.json", current, now, exact=False) is not None
            )
        if current["target"] is not None:
            allowed = current["target"] == self.host and local == current
        else:
            allowed = (
                self.host == self.secondary
                and current["host"] == self.primary
                and local["host"] == current["host"]
                and local["epoch"] == current["epoch"]
                and local["target"] is None
            )
        return (
            allowed and self._receipt_time("data-restored.json", local, now, exact=True) is not None
        )

    def eligible(self):
        return self._candidate(self._current(self.quorum))

    def accepts_restore(self):
        current = self._current(self.quorum)
        return bool(current and current["host"] != self.host and self._candidate(current))

    def expected_donor(self):
        current = self._current(self.quorum)
        if (
            current
            and current["host"] != self.host
            and (
                current["target"] == self.host
                or (
                    current["target"] is None
                    and self.host == self.secondary
                    and current["host"] == self.primary
                )
            )
        ):
            return current["host"]
        return None

    def _commit(self, owner, old, new):
        local = _read(self.root / "data-authority.json")
        write_receipt(
            self.root / "data-authority-intent.json",
            {"before_local": local, "after": new},
            history=False,
        )
        if not owner.compare(
            {AUTHORITY: canonical(old) if old else None}, {AUTHORITY: canonical(new)}
        ):
            raise QuorumUnavailable("data-host authority changed during the transition")
        write_receipt(self.root / "data-authority.json", new, history=False)

    def _finish_local(self, current):
        if _read(self.root / "data-authority.json") != current and self._local(current) == current:
            write_receipt(self.root / "data-authority.json", current, history=False)

    @staticmethod
    def _token():
        return digest(["private-state-version", uuid.uuid4().hex])

    def _new(self, owner, floor):
        return {
            "host": self.host,
            "epoch": self._token(),
            "tip": self._token(),
            "session": digest(["private-session", owner.token]),
            "floor": floor,
            "target": None,
        }

    def initialize(self, owner):
        if self.host != self.primary:
            raise CirraError("only the preferred data host can initialize private authority")
        current = self._current(owner)
        if current is None:
            if _read(self.root / "data-authority.json") is not None:
                raise CirraError("private state already belongs to a different authority")
            self._commit(owner, None, self._new(owner, _stamp()))
        elif (
            current["host"] != self.host
            or current != self._local(current)
            or self._restored_own_tenure(current)
        ):
            raise CirraError("existing authority differs from this primary's private state")
        else:
            self._finish_local(current)

    def activate(self, owner):
        current = self._current(owner)
        if not self._candidate(current):
            raise QuorumUnavailable(
                "worker needs a current verified copy of authoritative private state"
            )
        self._finish_local(current)
        if current["host"] != self.host:
            local = self._local(current)
            captured = self._receipt_time("data-restored.json", local, _stamp(), exact=True)
            if captured is None:
                raise QuorumUnavailable("the restored private state expired during promotion")
            self._commit(owner, current, self._new(owner, captured))
        elif current["session"] != digest(["private-session", owner.token]):
            self._advance(owner, current)
        elif not owner.compare({AUTHORITY: canonical(current)}, {}):
            raise QuorumUnavailable("another host owns this private state")

    def _advance(self, owner, current):
        self._commit(
            owner,
            current,
            {**current, "tip": self._token(), "session": digest(["private-session", owner.token])},
        )

    def checkpoint(self, owner):
        current = self._current(owner)
        if (
            not current
            or current["host"] != self.host
            or current["target"]
            or current["session"] != digest(["private-session", owner.token])
            or self._restored_own_tenure(current)
            or self._local(current) != current
        ):
            raise QuorumUnavailable("cannot checkpoint a different private state")
        self._finish_local(current)
        self._advance(owner, current)

    def freeze(self, owner, target_node):
        hosts = {self.config.preferred_node: self.primary, self.config.standby_node: self.secondary}
        target = hosts.get(target_node)
        if target is None or target == self.host:
            raise CirraError("transfer must select the other configured data host")
        current = self._current(owner)
        if (
            not current
            or current["host"] != self.host
            or self._restored_own_tenure(current)
            or self._local(current) != current
        ):
            raise CirraError("transfer requires the current authoritative private state")
        self._finish_local(current)
        if current["target"] != target:
            self._commit(owner, current, {**current, "tip": self._token(), "target": target})
        return {"frozen": self.config.node, "target": target_node}
