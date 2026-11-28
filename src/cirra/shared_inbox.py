"""Commit authenticated refresh jobs before HTTP 202 with private route data on the data hosts."""

import hashlib
import hmac
import json
import random
import re
import time

from cirra.domain import CirraError, DeliveryConflict, canonical, digest, number
from cirra.github import GitHubClient, native_id, record
from cirra.gitlab import GitLabClient
from cirra.job_queue import TERMINAL_REVIEW, QueueCapacity
from cirra.locking import change_lock
from cirra.publish import write_receipt
from cirra.quorum import OwnershipLost, QuorumUnavailable

PENDING = "inbox/pending/"
COUNT = "inbox/count"
CONFIG = "inbox/policy"
HEX = re.compile("[0-9a-f]{64}")


def intake_digest(route):
    return getattr(route, "intake_digest", None) or route.digest


def _comparison_attempts():
    """Bound contention retries without synchronizing competing receivers."""
    deadline = time.monotonic() + 2
    for attempt in range(8):
        if attempt:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, random.uniform(0.005, min(0.2, 0.01 * 2**attempt))))
            if time.monotonic() >= deadline:
                break
        yield attempt


class SharedInbox:
    def __init__(self, config, quorum, queue, credential):
        self.quorum, self.queue, self.credential = quorum, queue, credential
        self.routes = config.routes
        key = credential(config.coordination.routing_key_env)
        if not isinstance(key, str) or len(key.encode()) < 32:
            raise CirraError("shared intake needs its private routing key on both data hosts")
        self.secret = key.encode()
        self.policy = self.opaque(
            [
                "intake-v1",
                queue.maximum,
                config.max_jobs_per_day,
                config.max_attempts,
                config.coordination.preferred_node,
                config.coordination.standby_node,
                sorted(
                    (r.name, r.provider, r.repository_id, intake_digest(r))
                    for r in self.routes.values()
                ),
            ]
        )
        self.by_id = {self.route_id(route): route for route in self.routes.values()}
        self.cursor = ""
        self.provider_page = {}
        self.cache = queue.directory.parent / "coordinated-hints"

    def opaque(self, parts):
        return hmac.new(self.secret, canonical(parts).encode(), hashlib.sha256).hexdigest()

    def route_id(self, route):
        return self.opaque(["route", route.name, intake_digest(route)])

    def change_id(self, route, change):
        return self.opaque(["change", self.route_id(route), change])

    def initialize(self, owner):
        if not owner.compare({CONFIG: None, COUNT: None}, {CONFIG: self.policy, COUNT: "0"}):
            if owner.get(CONFIG) != self.policy or owner.get(COUNT) is None:
                raise CirraError(
                    "existing shared intake has a different policy or incomplete state"
                )

    def check(self):
        if self.quorum.get(CONFIG) != self.policy:
            raise QuorumUnavailable("shared intake has not been initialized with this policy")

    def enqueue(self, route, hint):
        try:
            result = self.admit(route, hint)
        except DeliveryConflict:
            raise
        except (CirraError, OSError):
            self.count_admission(hint, "refused")
            raise
        self.count_admission(hint, "duplicate" if result["duplicate"] else "accepted")
        return result

    def count_admission(self, hint, outcome):
        # The HTTP journal also records the outcome. A local metrics failure cannot
        # undo a successful admission or authorize an uncommitted one.
        try:
            with change_lock(self.queue.directory / "locks", "enqueue", 0.1):
                self.queue.record_admission(hint, outcome)
        except (CirraError, OSError, ValueError, TypeError, KeyError):
            pass

    def admit(self, route, hint):
        """The voter record alone identifies the accepted job even if this host vanishes."""
        change = number(hint["change"], "pull request number", 1, 2**31 - 1, integral=True)
        receipt = hint["payload_digest"]
        if not isinstance(receipt, str) or not HEX.fullmatch(receipt):
            raise CirraError("invalid authenticated payload digest")
        identifier = digest([route.name, receipt])
        change_id = self.change_id(route, change)
        delivery = self.opaque(["delivery", self.route_id(route), hint["delivery"]])
        value = canonical([self.route_id(route), change_id, receipt, delivery])
        job, binding, pending = (
            "inbox/jobs/" + identifier,
            "inbox/deliveries/" + delivery,
            PENDING + identifier,
        )
        # This index is private and inert. Only a committed pending record can schedule work.
        try:
            with change_lock(self.queue.directory / "locks", "enqueue", 0.1):
                self.remember(route, change)
        except (CirraError, OSError):
            pass  # The other data host can independently discover the private locator.
        for _ in _comparison_attempts():
            self.check()
            known = self.quorum.get(binding)
            prior = self.quorum.get(job)
            if known is not None and known != identifier:
                raise DeliveryConflict("webhook delivery identity was reused with another payload")
            if known is not None and prior is None:
                raise QuorumUnavailable("shared delivery is missing its committed job")
            checks = {CONFIG: self.policy, binding: known, job: prior}
            puts = {} if known else {binding: identifier}
            if prior is not None:
                # Different authentic deliveries can carry the same payload. Only the first
                # delivery is the canonical local queue hint, just as on a single host.
                original = self.quorum.get(pending) or self.quorum.get(
                    "inbox/settled/" + identifier
                )
                if original is None or digest(original) != prior:
                    raise QuorumUnavailable("shared job is missing its original admission")
                try:
                    operation = json.loads(original)
                except ValueError as exc:
                    raise QuorumUnavailable("invalid original admission") from exc
                if not isinstance(operation, list) or operation[:3] != [
                    self.route_id(route),
                    change_id,
                    receipt,
                ]:
                    raise QuorumUnavailable(
                        "duplicate delivery differs from its committed operation"
                    )
                if self.quorum.compare(checks, puts):
                    return {"job_id": identifier, "duplicate": True}
                continue
            count = self.quorum.get(COUNT)
            if count is None or not count.isdecimal():
                raise QuorumUnavailable("shared intake counter needs reconciliation")
            if int(count) >= self.queue.maximum:
                raise QuorumUnavailable("shared queue is full")
            checks.update(
                {
                    COUNT: count,
                    pending: None,
                    "inbox/results/" + identifier: None,
                    "inbox/settled/" + identifier: None,
                }
            )
            puts.update({COUNT: str(int(count) + 1), job: digest(value), pending: value})
            if self.quorum.compare(checks, puts):
                return {"job_id": identifier, "duplicate": False}
        raise QuorumUnavailable("shared intake is busy; retry this delivery")

    def remember(self, route, change):
        path = self.cache / (self.change_id(route, change) + ".json")
        if self.private_change(route, path.stem) is None:
            write_receipt(path, {"change": change}, history=False)

    def private_change(self, route, identity):
        path = self.cache / (identity + ".json")
        if not path.is_file():
            return None
        try:
            value = self.queue.read(path)
            change = number(value["change"], "cached change number", 1, 2**31 - 1, integral=True)
        except (CirraError, ValueError, TypeError, KeyError):
            return None
        return change if self.change_id(route, change) == identity else None

    def index_page(self, route):
        """Recover locators after a stale restore, even for closed changes."""
        github = route.provider == "github"
        client = (GitHubClient if github else GitLabClient)(
            route.repository, token=self.credential(route.reader_env) if route.reader_env else None
        )
        repository, _ = client.get()
        if native_id(record(repository).get("id")) != route.repository_id:
            raise CirraError("shared intake recovery repository changed identity")
        page = self.provider_page.get(route.name, 1)
        items, more = client.get(
            "pulls" if github else "merge_requests", {"state": "all", "per_page": 100, "page": page}
        )
        if not isinstance(items, list) or len(items) > 100:
            raise CirraError("shared intake recovery returned an invalid provider page")
        for item in items:
            data = record(item)
            repo_id = (
                record(record(data.get("base")).get("repo")).get("id")
                if github
                else data.get("project_id")
            )
            if native_id(repo_id) != route.repository_id:
                raise CirraError("shared intake recovery change belongs to another repository")
            change = number(
                data.get("number" if github else "iid"),
                "change number",
                1,
                2**31 - 1,
                integral=True,
            )
            self.remember(route, change)
        self.provider_page[route.name] = page + 1 if more else 1

    def settle(self, owner, identifier, state):
        if state not in TERMINAL_REVIEW:
            raise CirraError("shared work needs a terminal private queue record")
        key = PENDING + identifier
        for _ in _comparison_attempts():
            value, count = owner.get(key), owner.get(COUNT)
            if value is None:
                return
            if count is None or not count.isdecimal() or int(count) < 1:
                raise QuorumUnavailable("invalid shared pending count")
            if owner.compare(
                {
                    CONFIG: self.policy,
                    key: value,
                    COUNT: count,
                    "inbox/jobs/" + identifier: digest(value),
                },
                {
                    COUNT: str(int(count) - 1),
                    "inbox/results/" + identifier: state,
                    "inbox/settled/" + identifier: value,
                },
                deletes=(key,),
            ):
                return
        raise QuorumUnavailable("could not acknowledge the shared job")

    def materialize(self, owner):
        self.check()
        page, more = self.quorum.scan(PENDING, self.cursor)
        indexed = set()
        for key, raw in page:
            job_id = key.removeprefix(PENDING)
            try:
                fields = json.loads(raw)
            except ValueError as exc:
                raise QuorumUnavailable("invalid shared admission operation") from exc
            if (
                not isinstance(fields, list)
                or len(fields) != 4
                or not all(isinstance(x, str) and HEX.fullmatch(x) for x in fields)
            ):
                raise QuorumUnavailable("invalid shared admission operation")
            route_key, identity, payload, delivery = fields
            route = self.by_id.get(route_key)
            if route is None or digest([route.name, payload]) != job_id:
                raise QuorumUnavailable("shared admission disagrees with local route policy")
            local = self.queue.directory / (job_id + ".json")
            archived = self.queue.directory / "archive" / local.name
            entry = local if local.exists() else archived
            if entry.exists():
                job = self.queue.review_record(entry)
                if not self.matches(job, route, identity, payload):
                    raise QuorumUnavailable("private job differs from the committed operation")
                if job["state"] in TERMINAL_REVIEW:
                    self.settle(owner, job_id, job["state"])
                continue
            change = self.private_change(route, identity)
            if change is None and route_key not in indexed:
                try:
                    self.index_page(route)
                except (CirraError, OSError):
                    # This pending job will be retried. Other private, already resolved
                    # hints can still be served during an upstream outage.
                    indexed.add(route_key)
                    continue
                indexed.add(route_key)
                change = self.private_change(route, identity)
            if change is None:
                continue
            if not owner.compare(
                {key: raw, "inbox/jobs/" + job_id: digest(raw), CONFIG: self.policy}, {}
            ):
                continue
            hint = {
                "provider": route.provider,
                "instance": route.instance,
                "repository_id": route.repository_id,
                "repository": route.repository,
                "change": change,
                "delivery": "quorum:" + delivery,
                "payload_digest": payload,
            }
            existing = self.queue.directory / "completed" / (job_id + ".json")
            if existing.is_file():
                completion = self.queue.read(existing)
                if not isinstance(completion, dict) or completion.get("job_id") != job_id:
                    raise QuorumUnavailable(
                        "private completion receipt differs from the shared job"
                    )
                # Existing local/archive records are checked above, so legacy
                # receipts remain usable when their original record survives.
                # A receipt alone must not turn an unknown outcome into success.
                state = completion.get("state") or owner.get("inbox/results/" + job_id)
                if state not in TERMINAL_REVIEW:
                    raise QuorumUnavailable("private completion receipt lacks its terminal state")
                self.settle(owner, job_id, state)
                continue
            try:
                self.queue.enqueue(route, hint)
            except QueueCapacity:
                continue  # Keep the quorum job pending and let the worker free a private slot.
        self.cursor = page[-1][0] if more and page else ""

    def matches(self, job, route, identity, payload):
        hint = job["hint"]
        return (
            job["route"] == route.name
            and job["route_digest"] in (route.digest, intake_digest(route))
            and hint.get("payload_digest") == payload
            and self.change_id(route, hint.get("change")) == identity
        )

    def require_pending(self, owner, job):
        """A stale host can retain runnable private files after another host has settled them."""
        if owner is None:
            raise OwnershipLost("coordinated execution needs a current owner")
        if job["hint"].get("source"):
            return True  # Periodic reconciliation discovers this work independently.
        key = PENDING + job["id"]
        raw = owner.get(key)
        if raw is None:
            result = owner.get("inbox/results/" + job["id"])
            if result not in TERMINAL_REVIEW:
                raise QuorumUnavailable("restored job is missing its settlement record")
            if not owner.compare(
                {key: None, CONFIG: self.policy, "inbox/results/" + job["id"]: result}, {}
            ):
                raise OwnershipLost("pending job changed during recovery")
            return False
        route = self.routes.get(job["route"])
        hint = job["hint"]
        delivery = hint.get("delivery", "")
        if not route or not delivery.startswith("quorum:"):
            raise QuorumUnavailable("private job lacks its shared delivery identity")
        expected = canonical(
            [
                self.route_id(route),
                self.change_id(route, hint.get("change")),
                hint.get("payload_digest"),
                delivery.removeprefix("quorum:"),
            ]
        )
        if expected != raw or digest([route.name, hint.get("payload_digest")]) != job["id"]:
            raise QuorumUnavailable("private execution hint differs from the committed operation")
        if not owner.compare(
            {key: raw, "inbox/jobs/" + job["id"]: digest(raw), CONFIG: self.policy}, {}
        ):
            raise OwnershipLost("admission changed during worker takeover")
        return True
