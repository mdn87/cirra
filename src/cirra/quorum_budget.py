"""Daily admission counts and one-use work receipts guarded by the worker's quorum lease."""

import json
import re
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from cirra.domain import CirraError, canonical, digest, utc_now
from cirra.locking import change_lock
from cirra.publish import write_receipt
from cirra.queue_format import activate, live_format
from cirra.quorum import QuorumUnavailable, current_owner


def key(*parts):
    return "budgets/" + digest(parts)


EPOCH = key("epoch")
KINDS = {"review": "worker-usage", "audit": "audit-usage"}
ORIGIN = date(1970, 1, 1)
RETRIES = 16


def utc_day():
    return datetime.now(UTC).date()


def mutation_owner(directory):
    owner = current_owner()
    if owner is not None:
        activate(directory, 2)
    elif live_format(directory) >= 2:
        raise CirraError("coordinated daily admission requires a current publishing owner")
    return owner


def _date(day):
    if isinstance(day, str):
        day = date.fromisoformat(day)
    if type(day) is not date or day != utc_day():
        raise CirraError("shared daily admission requires the current UTC day")
    return day


def _count(value):
    if type(value) is not int or value < 0:
        raise CirraError("invalid shared daily admission count")
    return value


def _parse(raw):
    if raw is None:
        raise QuorumUnavailable("shared budgets have not been seeded from the primary")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise CirraError("invalid shared daily admission record") from exc
    if not isinstance(value, dict):
        raise CirraError("invalid shared daily admission record")
    return value


def _bucket(kind, day):
    return key("usage", kind, day.isoformat())


def _usage(raw):
    value = _parse(raw)
    return {"attempts": _count(value.get("attempts")), "maximum": _count(value.get("maximum"))}


def _path(root, kind, day):
    return Path(root) / KINDS[kind] / (day.isoformat() + ".json")


def _conflict():
    raise QuorumUnavailable(
        "shared daily admission changed repeatedly; retry with the current owner"
    )


@contextmanager
def enrollment_locks(root):
    root = Path(root).resolve(strict=True)
    with (
        change_lock(root / "queue" / "locks", "worker", 0),
        change_lock(root / "queue" / "locks", "enqueue", 0),
        change_lock(root / "audit-queue" / "locks", "audit-queue", 0),
    ):
        yield


def seed_budgets(
    owner, directory, day, review_limit, audit_limit, *, before_seed=None, after_seed=None
):
    """Take the two counts together, once, while the designated primary's queues are idle."""
    day = _date(day)
    root = Path(directory).resolve(strict=True)
    config = owner.quorum.config
    if config.node != config.preferred_node:
        raise CirraError("initial daily admission counts must come from the preferred primary")
    limits = {"review": _count(review_limit), "audit": _count(audit_limit)}
    seed = digest(["initial day", day.isoformat()])
    with enrollment_locks(root):
        if before_seed is not None:
            before_seed()
        existing = owner.get(EPOCH)
        marker = root / "quorum-budget-seed.json"
        if existing is not None:
            epoch = _parse(existing)
            seed = epoch.get("seed")
            if (
                not isinstance(seed, str)
                or not re.fullmatch("[0-9a-f]{64}", seed)
                or epoch.get("limits") != limits
            ):
                raise CirraError("shared budgets already have an initial day and policy")
            if marker.exists() and _parse(marker.read_text(encoding="utf-8")) != {
                "seed": seed,
                "limits": limits,
            }:
                raise CirraError("local seed receipt differs from the shared budget policy")
        else:
            if marker.exists():
                raise CirraError("previously seeded quorum is missing its admission history")
            checks, puts = {EPOCH: None}, {}
            for kind, maximum in limits.items():
                path = _path(root, kind, day)
                try:
                    record = _parse(path.read_text(encoding="utf-8"))
                    used = _count(record.get("attempts"))
                except FileNotFoundError:
                    used = 0  # A day without local admissions has no receipt.
                ledger = _bucket(kind, day)
                checks[ledger] = None
                puts[ledger] = canonical({"attempts": used, "maximum": maximum})
            # Count of UTC midnights since the Unix epoch is the monotonic rollover fence.
            puts[EPOCH] = canonical(
                {"seed": seed, "midnights": (day - ORIGIN).days, "limits": limits}
            )
            # A committed seed may lose its acknowledgement. Seal local spending first.
            activate(root, 2)
            if not owner.compare(checks, puts):
                raise CirraError("shared budget initialization was not confirmed; verify and retry")
        activate(root, 2)
        usage = sync_usage(owner, root, day)
        if not marker.exists():
            write_receipt(marker, {"seed": seed, "limits": limits}, history=False)
        if after_seed is not None:
            after_seed()
        return usage


def _epoch(owner, day):
    day = _date(day)
    tick = (day - ORIGIN).days
    for _ in range(RETRIES):
        raw = owner.get(EPOCH)
        record = _parse(raw)
        previous = _count(record.get("midnights"))
        limits = record.get("limits")
        if not isinstance(limits, dict) or set(limits) != set(KINDS):
            raise CirraError("invalid shared daily admission policy")
        for value in limits.values():
            _count(value)
        if tick < previous:
            raise CirraError("shared daily admission clock moved behind the active UTC day")
        if tick == previous:
            return raw, limits
        checks = {EPOCH: raw}
        record["midnights"] = tick
        puts = {EPOCH: canonical(record)}
        for kind, maximum in limits.items():
            previous_key = _bucket(kind, ORIGIN + timedelta(days=previous))
            previous_raw = owner.get(previous_key)
            if _usage(previous_raw)["maximum"] != maximum:
                raise CirraError("previous admission bucket differs from the seeded policy")
            checks[previous_key] = previous_raw
            ledger = _bucket(kind, day)
            checks[ledger] = None
            puts[ledger] = canonical({"attempts": 0, "maximum": maximum})
        if owner.compare(checks, puts):
            return puts[EPOCH], limits
    _conflict()


def _mirror(path, usage):
    try:
        record = _parse(path.read_text(encoding="utf-8"))
        previous = {"attempts": record.get("attempts"), "maximum": record.get("maximum")}
    except FileNotFoundError:
        previous = None
    if previous != usage:
        write_receipt(path, {**usage, "updated_at": utc_now()})


def sync_usage(owner, directory, day):
    """Refresh both legacy status files from one owner-checked snapshot of the daily counts."""
    day = _date(day)
    root = Path(directory)
    with change_lock(root / "queue" / "locks", "budget-status", 2):
        for _ in range(RETRIES):
            epoch, limits = _epoch(owner, day)
            checks, values = {EPOCH: epoch}, {}
            for kind, maximum in limits.items():
                ledger = _bucket(kind, day)
                raw = owner.get(ledger)
                values[kind] = _usage(raw)
                if values[kind]["maximum"] != maximum:
                    raise CirraError("shared daily admission bucket differs from the seeded policy")
                checks[ledger] = raw
            if owner.compare(checks, {}):
                for kind, usage in values.items():
                    _mirror(_path(root, kind, day), usage)
                return values
        _conflict()


def operation(kind, identity, sequence):
    return key("attempt", kind, identity, _count(sequence))


def _receipt(raw):
    value = _parse(raw)
    if (
        value.get("phase") not in ("charged", "resumed", "returned")
        or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("owner", "")))
        or not re.fullmatch(r"budgets/[0-9a-f]{64}", str(value.get("ledger", "")))
    ):
        raise CirraError("invalid shared work reservation")
    return value


def reserve(owner, root, kind, day, maximum, ticket, *, previous=None):
    """A duplicate ticket never authorizes a second execution, including after a lost reply."""
    day = _date(day)
    ledger = _bucket(kind, day)
    for _ in range(RETRIES):
        epoch, limits = _epoch(owner, day)
        if _count(maximum) != limits[kind]:
            raise CirraError("local daily admission limit differs from the seeded policy")
        raw = owner.get(ledger)
        usage = _usage(raw)
        if usage["maximum"] != maximum:
            raise CirraError("shared daily admission bucket differs from policy")
        old = owner.get(ticket)
        checks = {EPOCH: epoch, ledger: raw, ticket: old}
        puts = {}
        if old is not None:
            _receipt(old)
            outcome = "uncertain"
        else:
            same_day = False
            if previous:
                prior = owner.get(previous)
                if prior is None:
                    raise CirraError("prior shared audit admission cannot be verified")
                evidence = _receipt(prior)
                checks[previous] = prior
                same_day = evidence["ledger"] == ledger and evidence["phase"] != "returned"
            if not same_day and usage["attempts"] >= maximum:
                outcome = "full"
            else:
                outcome = "resumed" if same_day else "charged"
                puts[ticket] = canonical(
                    {"phase": outcome, "owner": digest(owner.token), "ledger": ledger}
                )
                if not same_day:
                    puts[ledger] = canonical({**usage, "attempts": usage["attempts"] + 1})
        if owner.compare(checks, puts):
            sync_usage(owner, root, utc_day())
            return outcome
    _conflict()


def refund(owner, root, kind, day, ticket):
    """Only the acquisition that charged this proven-unused attempt can return its original slot."""
    ledger = _bucket(kind, day)
    for _ in range(RETRIES):
        old = owner.get(ticket)
        receipt = _receipt(old)
        raw = owner.get(ledger)
        usage = _usage(raw)
        if ledger != receipt["ledger"]:
            raise CirraError("refund does not refer to the original budget day")
        checks = {ticket: old, ledger: raw}
        puts = {}
        if receipt["owner"] == digest(owner.token) and receipt["phase"] == "charged":
            if not usage["attempts"]:
                raise CirraError("charged work has no recorded daily admission")
            puts = {
                ticket: canonical({**receipt, "phase": "returned"}),
                ledger: canonical({**usage, "attempts": usage["attempts"] - 1}),
            }
        if owner.compare(checks, puts):
            # A late refund belongs to yesterday even if the current day has already rolled.
            if day == utc_day():
                sync_usage(owner, root, day)
            else:
                with change_lock(Path(root) / "queue" / "locks", "budget-status", 2):
                    _mirror(_path(root, kind, day), _usage(owner.get(ledger)))
            return bool(puts)
    _conflict()
