"""Bind each native publication slot to a durable intent on the active worker's quorum."""

from contextlib import contextmanager
from contextvars import ContextVar

from cirra.domain import CirraError, ProviderAccessDenied, ProviderRateLimited, digest
from cirra.quorum import current_owner
from cirra.transport import RemoteUncertain, RequestNotSent

VACANT = digest(["no native object"])
_SEND = ContextVar("cirra_reserved_native_send", default=None)


def require_state_owner(directory):
    from cirra.coordination import shared_state

    if current_owner() is None and shared_state(directory):
        raise CirraError("publishing from this state requires the active quorum worker")


def require_send(kind, slot, proof, target):
    """Direct adapter calls must consume the same single-use send permit as worker calls."""
    owner = current_owner()
    if owner is not None:
        if _SEND.get() != (owner, "effects/" + digest([kind, slot]), proof, target):
            raise RemoteUncertain("no quorum reservation covers this native write")
        _SEND.set(None)


class PublicationFence:
    def __init__(self, kind, slot):
        self.owner = current_owner()
        self.key = "effects/" + digest([kind, slot])
        self.previous = None
        self.checked = False

    def check_binding(self):
        if self.owner is not current_owner():
            raise RemoteUncertain("native publication changed worker ownership")

    def compare(self, before, after):
        try:
            applied = self.owner.compare(
                {self.key: before}, {self.key: after} if before != after else {}
            )
        except CirraError as exc:
            raise RemoteUncertain("publication quorum did not confirm the operation") from exc
        if not applied:
            raise RemoteUncertain("publication ownership or the slot generation changed")

    def observe(self, identifier, proof):
        """Return true only for exact readback of an operation already recorded on quorum."""
        self.check_binding()
        if self.owner is None:
            return False
        try:
            prior = self.owner.get(self.key)
        except CirraError as exc:
            raise RemoteUncertain("publication quorum could not read the operation") from exc
        known = False
        if identifier is None:
            if prior not in (None, VACANT):
                raise RemoteUncertain("quorum recorded a native operation that is not visible")
            observed = prior
        else:
            if not proof:
                raise RemoteUncertain("native publication has no usable readback fingerprint")
            # A legacy object seen before enrollment must not turn into permission to
            # create if a later GET is empty. Seeing it alone does not attest its body.
            legacy = digest(["legacy", identifier, proof])
            observed = digest(["confirmed", identifier, proof])
            known = prior in (
                digest(["pending", None, proof]),
                digest(["pending", identifier, proof]),
                observed,
            )
            if not known:
                if prior not in (None, legacy):
                    raise RemoteUncertain("native readback differs from the quorum operation")
                observed = legacy
        self.compare(prior, observed)
        self.previous, self.checked = observed, True
        return known

    @contextmanager
    def writing(self, proof, identifier, *, prepare=None):
        """Run the local checkpoint, then grant one send. An uncertain send keeps its slot."""
        self.check_binding()
        if self.owner is None:
            if prepare is not None:
                prepare()
            yield
            return
        if not self.checked or not proof:
            raise RemoteUncertain("native slot must be observed before a write")
        pending = digest(["pending", identifier, proof])
        self.compare(self.previous, pending)
        self.checked = False
        if prepare is not None:
            try:
                prepare()
            except Exception:
                # The adapter has not received its send permit. Release only by
                # comparing both this reservation and the original owner's lease.
                self.compare(pending, self.previous or VACANT)
                raise
        token = _SEND.set((self.owner, self.key, proof, identifier))
        try:
            yield
        except (RequestNotSent, ProviderRateLimited, ProviderAccessDenied):
            # These are verified pre-send or explicit provider refusals. A timeout,
            # malformed acknowledgement, or lost owner can never release the slot.
            self.compare(pending, self.previous or VACANT)
            raise
        finally:
            _SEND.reset(token)

    def acknowledged(self, proof, identifier, *, target):
        self.check_binding()
        if self.owner is not None:
            if target is not None and identifier != target:
                raise RemoteUncertain("provider acknowledged a different native object")
            confirmed = digest(["confirmed", identifier, proof])
            self.compare(digest(["pending", target, proof]), confirmed)
            self.previous, self.checked = confirmed, True
