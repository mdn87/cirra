"""Atomic file-backed deliveries; one process-lifetime worker lock, no DB migration."""

import json
import math
import os
import re
import time
import uuid
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path

from cirra.domain import CirraError, digest, number, string, utc_now
from cirra.locking import change_lock
from cirra.publish import receipt_directory, sync_directory, write_receipt
from cirra.queue_format import activate
from cirra.queue_slots import Slots, hot_paths

TERMINAL_REVIEW = ("done", "attention", "skipped")


class Queue:
    def __init__(self, directory, maximum=10000):
        self.directory = Path(directory).resolve() / "queue"
        receipt_directory(self.directory)
        self.maximum = maximum
        self.slots = Slots(self.directory)
        self._sweep = None
        self._census = None
        self._next_sweep = 0

    def read(self, path):
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise CirraError("queue record exceeds byte limit")
        return json.loads(raw)

    def save(self, job):
        activate(self.directory.parent)
        job["schema_version"] = 1
        write_receipt(self.directory / (job["id"] + ".json"), job)
        if job["state"] in TERMINAL_REVIEW:
            self.remember_completion(job)
            if job["state"] != "attention":
                self.remember_reclaimable(job)

    def remember_reclaimable(self, job):
        path = self.directory / "reclaimable" / (job["id"] + ".json")
        if not path.exists():
            write_receipt(path, {"id": job["id"]}, history=False)

    def remember_completion(self, job):
        path = self.directory / "completed" / (job["id"] + ".json")
        if not path.exists():
            write_receipt(
                path, {"job_id": job["id"], "accepted_at": job["accepted_at"]}, history=False
            )

    def archive_finished(self, jobs, *, pressure=False):
        """Preserve terminal records outside the polling set; keep payload replay evidence."""
        for job in jobs:
            with change_lock(self.directory / "locks", "enqueue", 2):
                self._archive_locked(job["id"], pressure=pressure)

    def _archive_locked(self, identifier, *, pressure):
        path = self.directory / (identifier + ".json")
        candidate = self.directory / "reclaimable" / path.name
        if not path.exists():
            candidate.unlink(missing_ok=True)
            return False
        try:
            job = self.review_record(path)
        except (CirraError, ValueError, OverflowError, RecursionError):
            # The malformed job keeps its slot and remains visible to health.
            candidate.unlink(missing_ok=True)
            return False
        if job["state"] not in TERMINAL_REVIEW:
            candidate.unlink(missing_ok=True)
            return False
        if (not pressure or job["state"] == "attention") and (
            datetime.fromisoformat(job["updated_at"]).timestamp()
            >= time.time() - ARCHIVE_AFTER_SECONDS
        ):
            return False
        archive = self.directory / "archive" / path.name
        activate(self.directory.parent)
        self.remember_completion(job)
        receipt_directory(archive.parent)
        self.slots.record(self.slots.count(), operation="remove", identifier=identifier)
        path.replace(archive)
        sync_directory(archive.parent)
        sync_directory(self.directory)
        candidate.unlink(missing_ok=True)
        return True

    def reclaim_one(self):
        index = self.directory / "reclaimable"
        if index.is_dir():
            with os.scandir(index) as entries:
                for entry in islice(entries, 4):
                    if re.fullmatch(r"[a-f0-9]{64}\.json", entry.name):
                        if self._archive_locked(entry.name[:-5], pressure=True):
                            return True
        return False

    def available_slot(self):
        """Under the enqueue lock, return the count to reserve against if a slot is free."""
        count = self.slots.count()
        if count == self.maximum and self.reclaim_one():
            count -= 1
        return count if count is not None and count < self.maximum else None

    def maintain(self):
        """Bound each pass to 512 filenames and 32 job reads; return False during initial census."""
        if self._sweep is None:
            if time.monotonic() < self._next_sweep:
                return True
            if self._census is None:
                with change_lock(self.directory / "locks", "enqueue", 2):
                    self._before_census = self.slots.version()
                self._census = hot_paths(self.directory)
                self._census_count = 0
            batch = sum(1 for _ in islice(self._census, 512))
            self._census_count += batch
            if batch == 512:
                return self.slots.path.exists()
            with change_lock(self.directory / "locks", "enqueue", 2):
                if self.slots.version() == self._before_census:
                    self.slots.record(self._census_count)
            self._census = None
            self._sweep = hot_paths(self.directory)
        for _ in range(32):
            path = next(self._sweep, None)
            if path is None:
                self._sweep = None
                self._next_sweep = time.monotonic() + 86400
                break
            try:
                job = self.review_record(path)
            except (OSError, ValueError, OverflowError, RecursionError, CirraError):
                continue
            if job["state"] in TERMINAL_REVIEW:
                if (
                    datetime.fromisoformat(job["updated_at"]).timestamp()
                    < time.time() - ARCHIVE_AFTER_SECONDS
                ):
                    self.archive_finished([job])
                elif job["state"] != "attention":
                    self.remember_reclaimable(job)
                    if (self.slots.count() or 0) >= self.maximum:
                        self.archive_finished([job], pressure=True)
        return True

    def record_admission(self, hint, outcome):
        if hint.get("source"):
            return
        day = datetime.now(UTC).date().isoformat()
        path = self.directory.parent / "webhook-admissions" / f"{day}.json"
        counts = self.read(path) if path.exists() else {"accepted": 0, "duplicate": 0, "refused": 0}
        counts[outcome] += 1
        counts["updated_at"] = utc_now()
        write_receipt(path, counts, history=False)

    def enqueue(self, route, hint):
        identifier = digest([route.name, hint["payload_digest"]])
        delivery = digest([route.name, hint["delivery"]])
        with change_lock(self.directory / "locks", "enqueue", 2):
            delivery_path = self.directory / "deliveries" / (delivery + ".json")
            if delivery_path.exists() and self.read(delivery_path)["job_id"] != identifier:
                raise CirraError("webhook delivery identity was reused with another payload")
            path = self.directory / (identifier + ".json")
            archived = self.directory / "archive" / path.name
            if (
                path.exists()
                or archived.exists()
                or (self.directory / "completed" / path.name).exists()
            ):
                if not delivery_path.exists():
                    write_receipt(
                        delivery_path,
                        {"job_id": identifier, "recorded_at": utc_now()},
                        history=False,
                    )
                self.record_admission(hint, "duplicate")
                return {"job_id": identifier, "duplicate": True}
            count = self.available_slot()
            if count is None:
                self.record_admission(hint, "refused")
                write_receipt(
                    self.directory.parent / "queue-refusal.json",
                    {"observed_at": utc_now(), "maximum": self.maximum},
                    history=False,
                )
                raise CirraError("queue capacity reached; retry after active work finishes")
            job = {
                "schema_version": 1,
                "id": identifier,
                "route": route.name,
                "route_digest": route.digest,
                "reader_backoff": self.reader_key(route),
                "hint": hint,
                "state": "queued",
                "attempts": 0,
                "not_before": 0,
                "accepted_at": utc_now(),
                "updated_at": utc_now(),
            }
            # Record delivery identity first: a crash can be retried without losing the job.
            write_receipt(
                delivery_path, {"job_id": identifier, "recorded_at": utc_now()}, history=False
            )
            self.slots.record(count, operation="add", identifier=identifier)
            self.save(job)
            self.record_admission(hint, "accepted")
            return {"job_id": identifier, "duplicate": False}

    def review_record(self, path):
        job = self.read(path)
        if (
            not isinstance(job, dict)
            or type(job.get("schema_version", 1)) is not int
            or job.get("schema_version", 1) != 1
            or job.get("id") != path.stem
            or not re.fullmatch(r"[a-f0-9]{64}", path.stem)
            or job.get("state") not in ("queued", "running", "retry", *TERMINAL_REVIEW)
            or type(job.get("attempts")) is not int
            or job["attempts"] < 0
            or type(job.get("not_before")) not in (int, float)
            or not math.isfinite(job["not_before"])
            or not all(
                isinstance(job.get(k), str)
                for k in ("route", "route_digest", "accepted_at", "updated_at")
            )
            or not isinstance(job.get("hint"), dict)
            or not isinstance(job.get("result", {}), dict)
            or (
                "reader_backoff" in job
                and (
                    not isinstance(job["reader_backoff"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", job["reader_backoff"])
                )
            )
        ):
            raise ValueError("invalid review queue record")
        for key in ("accepted_at", "updated_at"):
            if datetime.fromisoformat(job[key]).tzinfo is None:
                raise ValueError("queue timestamp lacks a timezone")
        hint = job["hint"]
        if "branch" in hint and (not isinstance(hint["branch"], str) or not hint["branch"]):
            raise ValueError("invalid review branch")
        if "change" in hint and not (
            (type(hint["change"]) is int and 0 < hint["change"] <= 2**31 - 1)
            or ("branch" in hint and hint["change"] == "branch:" + hint["branch"])
        ):
            raise ValueError("invalid review change")
        for key in ("ci", "review_gate"):
            if not isinstance(job.get("result", {}).get(key) or {}, dict):
                raise ValueError("invalid review result")
        return job

    def scan(self):
        """Readable job records, and names of records that could not be read."""
        jobs, unreadable = [], []
        for path in self.directory.glob("*.json"):
            try:
                jobs.append(self.review_record(path))
            except (OSError, ValueError, OverflowError, RecursionError, CirraError):
                unreadable.append(path.name)
        return jobs, sorted(unreadable)

    @staticmethod
    def reader_key(route):
        # Credential references are operator-owned. The secret itself never enters state.
        if not hasattr(route, "provider"):
            return digest([route.name, route.digest])
        return digest(
            [route.provider, getattr(route, "instance", None), getattr(route, "reader_env", None)]
        )

    def cooldown(self, key, cache=None):
        if cache is not None and key in cache:
            return cache[key]
        path = self.directory.parent / "provider-backoff" / (key + ".json")
        try:
            until = self.read(path)["until"] if path.exists() else 0
            if type(until) not in (int, float) or not math.isfinite(until):
                raise ValueError("invalid provider cooldown")
        except (CirraError, KeyError, OSError, RecursionError, TypeError, ValueError):
            until = math.inf  # Hold only affected work until the record can be repaired.
        if cache is not None:
            cache[key] = until
        return until

    def eligible_at(self, job, cache, routes=None):
        route_key = digest([job["route"], job["route_digest"]])
        route = (routes or {}).get(job["route"])
        reader_key = self.reader_key(route) if route else job.get("reader_backoff", route_key)
        return max(
            job["not_before"], self.cooldown(route_key, cache), self.cooldown(reader_key, cache)
        )

    def pending(self, routes=None):
        # An unreadable record is skipped (and reported by `cirra health`)
        # rather than crashing the worker loop on every pass.
        jobs, _ = self.scan()
        now = time.time()
        cooldowns = {}
        return sorted(
            (
                job
                for job in jobs
                if job["state"] in ("queued", "running", "retry")
                and self.eligible_at(job, cooldowns, routes) <= now
            ),
            key=lambda job: (job["accepted_at"], job["id"]),
        )

    def tested_head(self, route, change, head, *, review_target=None, review_merge=None):
        """Whether a finished job on this route already tested `head` for `change`."""
        for job in self.scan()[0]:
            result = job.get("result") or {}
            ci = result.get("ci") or {}
            gate = result.get("review_gate") or {}
            if (
                job.get("route") == route.name
                and job.get("route_digest") == route.digest
                and job.get("state") == "done"
                and ci.get("change") == change
                and ci.get("head_sha") == head
                and (
                    review_target is None
                    or (
                        gate.get("state") == "published"
                        and gate.get("head_sha") == head
                        and gate.get("target_sha") == review_target
                        and review_merge is not None
                        and gate.get("merge_sha") == review_merge
                    )
                )
            ):
                return True
        return False

    def request(self, route, change, *, source="manual", revision=None):
        number(change, "change number", 1, 2**31 - 1, integral=True)
        identity = revision if revision is not None else uuid.uuid4().hex
        key = digest([source, route.digest, change, identity])
        return self.enqueue(
            route,
            {
                "provider": route.provider,
                "instance": route.instance,
                "repository_id": route.repository_id,
                "repository": route.repository,
                "change": change,
                "delivery": source + ":" + key,
                "payload_digest": key,
                "source": source,
            },
        )

    def requeue(self, identifier, route, reason):
        if not re.fullmatch(r"[a-f0-9]{64}", identifier):
            raise CirraError("invalid job identifier")
        string(reason, "requeue reason", 1000)
        # Serialize operator recovery. Active jobs are refused; the worker only
        # selects queued/running/retry jobs, so terminal records are safe to reset.
        with change_lock(self.directory / "locks", "enqueue", 2):
            path = self.directory / (identifier + ".json")
            archived = self.directory / "archive" / path.name
            source = path if path.is_file() else archived
            if not source.is_file():
                raise CirraError("job does not exist")
            job = self.review_record(source)
            if job["route"] != route.name:
                raise CirraError("job belongs to a different route")
            if job["state"] not in ("done", "attention", "skipped"):
                raise CirraError("only terminal jobs may be requeued")
            if source == archived:
                count = self.available_slot()
                if count is None:
                    raise CirraError("queue capacity reached; retry after active work finishes")
                self.slots.record(count, operation="add", identifier=identifier)
            write_receipt(
                self.directory / "history" / (identifier + "-" + uuid.uuid4().hex + ".json"),
                {"previous": job, "reason": reason, "recorded_at": utc_now()},
                history=False,  # this record is the archive; no nested copy
            )
            job = {
                **job,
                "state": "queued",
                "attempts": 0,
                "not_before": 0,
                "route_digest": route.digest,
                "reader_backoff": self.reader_key(route),
                "updated_at": utc_now(),
            }
            job.pop("error", None)
            job.pop("result", None)
            self.save(job)
            return job

    def request_branch(self, route, branch, *, revision=None):
        if branch not in route.branches:
            raise CirraError("branch is not allowlisted on this route")
        key = digest(["branch", route.digest, branch, revision or uuid.uuid4().hex])
        return self.enqueue(
            route,
            {
                "provider": route.provider,
                "instance": route.instance,
                "repository_id": route.repository_id,
                "repository": route.repository,
                "branch": branch,
                "change": "branch:" + branch,
                "delivery": "branch:" + key,
                "payload_digest": key,
                "source": "branch",
            },
        )

    def status(self):
        jobs, _ = self.scan()
        return [
            {
                key: job.get(key)
                for key in ("id", "route", "state", "attempts", "updated_at", "result", "error")
            }
            for job in jobs
        ]

    def usage_path(self):
        day = datetime.now(UTC).date().isoformat()
        return self.directory.parent / "worker-usage" / (day + ".json")

    def usage(self):
        path = self.usage_path()
        return self.read(path) if path.exists() else {"attempts": 0}

    def defer_route(self, route, until, *, reader=False):
        key = self.reader_key(route) if reader else digest([route.name, route.digest])
        path = self.directory.parent / "provider-backoff" / (key + ".json")
        with change_lock(self.directory / "locks", "backoff:" + key, 2):
            previous = self.cooldown(key)
            if math.isfinite(previous):
                until = max(until, previous)
            write_receipt(
                path, {"until": until, "route": route.name, "updated_at": utc_now()}, history=False
            )

    def defer_reader(self, route, exc):
        until = max(time.time() + 1, getattr(exc, "retry_at", time.time() + 900))
        self.defer_route(route, until, reader=True)
        return until

    def reserve_attempt(self, maximum, path=None):
        # The caller holds the process-lifetime worker lock. Persist before work
        # so a crash cannot reset the daily admission limit.
        path = self.usage_path() if path is None else path
        usage = self.read(path) if path.exists() else {"attempts": 0}
        if usage["attempts"] >= maximum:
            return False
        write_receipt(
            path,
            {"attempts": usage["attempts"] + 1, "maximum": maximum, "updated_at": utc_now()},
        )
        return True

    def return_unused_attempt(self, path):
        usage = self.read(path)
        usage.update(attempts=max(0, usage["attempts"] - 1), updated_at=utc_now())
        write_receipt(path, usage)


AUDIT_PRIORITY = {"release": 0, "pr": 1, "full": 2}
ACTIVE_AUDIT = (
    "queued",
    "deferred",
    "running",
    "awaiting_import",
    "awaiting_baseline",
    "awaiting_publication",
)
# Finished records move to audit-queue/archive after this long, so each request
# scans only recent and active work; completed results stay attachable in SQLite.
ARCHIVE_AFTER_SECONDS = 7 * 86400
ARCHIVED_STATES = ("done", "superseded", "cancelled", "failed", "attention")
# Every field the queue reads from a record; a record missing one is reported.
RECORD_STRINGS = ("work_key", "audit_id", "scope", "reason", "accepted_at", "updated_at")


class AuditQueue:
    """Production-audit requests: one queued and one running request per work key.

    A work key is repository, scope (change, release ref, or the repository for
    full audits) and tier. Newer requests coalesce queued ones and flag running
    ones as superseded; identical identities attach to existing work.
    """

    def __init__(self, directory, store=None, maximum=10000):
        self.directory = Path(directory).resolve() / "audit-queue"
        receipt_directory(self.directory)
        self.store, self.maximum = store, maximum

    def _lock(self):
        return change_lock(self.directory / "locks", "audit-queue", 10)

    def read(self, identifier):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise CirraError("invalid audit job identifier")
        path = self.directory / (identifier + ".json")
        if not path.is_file():
            raise CirraError("audit job does not exist")
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, job):
        activate(self.directory.parent)
        job["schema_version"] = 1
        job["updated_at"] = utc_now()
        write_receipt(self.directory / (job["id"] + ".json"), job)

    def scan(self):
        """Readable job records, and names of records that could not be read."""
        jobs, unreadable = [], []
        for path in self.directory.glob("*.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if not (
                    isinstance(job, dict)
                    and type(job.get("schema_version", 1)) is int
                    and job.get("schema_version", 1) == 1
                    and type(job.get("sequence")) is int
                    and job.get("id") == path.stem
                    and re.fullmatch(r"[a-f0-9]{32}", path.stem)
                    and job.get("state") in (*ACTIVE_AUDIT, *ARCHIVED_STATES)
                    and isinstance(job.get("identity"), dict)
                    and isinstance(job["identity"].get("repository"), dict)
                    and all(
                        isinstance(job["identity"]["repository"].get(key), str)
                        for key in ("provider", "instance", "repository_id")
                    )
                    and all(
                        isinstance(job["identity"].get(key), str)
                        for key in ("skill_version", "policy_digest", "catalog_digest")
                    )
                    and isinstance(job["identity"].get("head"), str)
                    and isinstance(job.get("tier"), str)
                    and job["tier"] in AUDIT_PRIORITY
                    and job["identity"].get("tier") == job["tier"]
                    and "base" in job["identity"]
                    and all(isinstance(job.get(key), str) for key in RECORD_STRINGS)
                    and re.fullmatch("[a-f0-9]{64}", job["work_key"])
                    and re.fullmatch("[a-f0-9]{64}", job["audit_id"])
                    and type(job.get("not_before")) in (int, float)
                    and math.isfinite(job["not_before"])
                    and isinstance(job.get("result", {}), dict)
                    and type(job.get("publication_tries", 0)) is int
                    and isinstance(job.get("cancel_requested"), bool)
                    and isinstance(job.get("context"), dict)
                    and (
                        job["context"].get("route") is None
                        or isinstance(job["context"]["route"], str)
                    )
                    and (job.get("superseded_by") is None or isinstance(job["superseded_by"], str))
                    and (
                        job.get("attempt_id") is None
                        or (
                            isinstance(job["attempt_id"], str)
                            and re.fullmatch("[a-f0-9]{32}", job["attempt_id"])
                        )
                    )
                ):
                    raise ValueError("audit record lacks required fields")
                for key in ("accepted_at", "updated_at"):
                    if datetime.fromisoformat(job[key]).tzinfo is None:
                        raise ValueError("audit timestamp lacks a timezone")
            except (OSError, ValueError, OverflowError, RecursionError):
                unreadable.append(path.name)
                continue
            jobs.append(job)
        return sorted(jobs, key=lambda job: (job["sequence"], job["id"])), sorted(unreadable)

    def jobs(self):
        # Unreadable records are skipped here and reported by `cirra health`.
        return self.scan()[0]

    def archive(self, jobs, now):
        """Move finished records older than ARCHIVE_AFTER_SECONDS out of the scan."""
        kept = []
        for job in jobs:
            updated = datetime.fromisoformat(job["updated_at"]).timestamp()
            if job["state"] in ARCHIVED_STATES and now - updated > ARCHIVE_AFTER_SECONDS:
                target = self.directory / "archive" / (job["id"] + ".json")
                self.remember_request(job)
                receipt_directory(target.parent)
                (self.directory / (job["id"] + ".json")).replace(target)
                sync_directory(target.parent)
                sync_directory(self.directory)
            else:
                kept.append(job)
        return kept

    def remember_request(self, job):
        # The latest requested scope survives moving terminal evidence off this volume.
        path = self.directory / "latest" / (job["work_key"] + ".json")
        if path.exists():
            latest = json.loads(path.read_text(encoding="utf-8"))
            if latest["accepted_at"] >= job["accepted_at"]:
                return
        write_receipt(path, {"id": job["id"], "accepted_at": job["accepted_at"]}, history=False)

    def _mark_attempt(self, job, state):
        """Fence the job's attempt; returns the attempt's resulting state, if any."""
        if self.store is not None and job.get("attempt_id"):
            from cirra.audits import mark

            return mark(self.store, job["attempt_id"], state)
        return None

    def request(
        self, identity, scope, *, reason, debounce_seconds=0, context=None, now=None, fresh=False
    ):
        """Queue, coalesce or attach one audit request.

        `fresh` requests a new run even when the identity already completed
        (scheduled and stale re-audits); it still attaches to active work.
        """
        from cirra.audits import repository_key

        string(scope, "audit scope", 300)
        string(reason, "audit reason", 200)
        now = time.time() if now is None else now
        audit_id = digest(identity)
        work_key = digest([repository_key(identity["repository"]), scope, identity["tier"]])
        with self._lock():
            jobs = self.archive(self.jobs(), time.time())
            for job in jobs:
                if (
                    job["audit_id"] == audit_id
                    and job["scope"] == scope
                    and job["state"] in (ACTIVE_AUDIT if fresh else (*ACTIVE_AUDIT, "done"))
                    and not job["cancel_requested"]
                    and not job["superseded_by"]
                ):
                    return {"job_id": job["id"], "attached": True}
            # Same-key work the new request supersedes leaves as it arrives.
            active = sum(
                job["state"] in ACTIVE_AUDIT and job["work_key"] != work_key for job in jobs
            )
            if active >= self.maximum:
                raise CirraError("audit queue capacity reached; cancel or finish queued audits")
            if self.store is not None and not fresh:
                from cirra.audits import begin, completed

                attempt = completed(self.store, audit_id)
                if attempt:
                    if scope == "repository":
                        begin(self.store, identity, scope)  # The commit is now the baseline.
                    return {"job_id": None, "attempt_id": attempt, "attached": True}
            job = {
                "schema_version": 1,
                "id": uuid.uuid4().hex,
                "sequence": max((item["sequence"] for item in jobs), default=0) + 1,
                "work_key": work_key,
                "audit_id": audit_id,
                "identity": identity,
                "tier": identity["tier"],
                "scope": scope,
                "reason": reason,
                "context": context or {},
                "state": "queued",
                "attempt_id": None,
                "superseded_by": None,
                "cancel_requested": False,
                "not_before": now + debounce_seconds,
                "accepted_at": utc_now(),
                "fresh": fresh,
            }
            for other in jobs:
                if other["work_key"] != work_key:
                    continue
                if other.get("attempt_id") and other["state"] in ACTIVE_AUDIT:
                    # A newer head can reuse unchanged domains of this attempt.
                    job["resume_from"] = other["attempt_id"]
                if other["state"] in ("queued", "deferred"):
                    other.update(state="superseded", superseded_by=job["id"])
                    self._mark_attempt(other, "superseded")  # A preempted full audit.
                    self.save(other)
                elif (
                    other["state"]
                    in ("running", "awaiting_import", "awaiting_baseline", "awaiting_publication")
                    and not other["superseded_by"]
                ):
                    # The runner stops at its next checkpoint; the attempt's state is
                    # the fence that refuses a late import of the old head.
                    other["superseded_by"] = job["id"]
                    self._mark_attempt(other, "superseded")
                    self.save(other)
            self.save(job)
            self.remember_request(job)
            return {"job_id": job["id"], "attached": False}

    @staticmethod
    def blocked(jobs):
        """Repositories whose finished pr audits wait on a first baseline."""
        return {
            json.dumps(job["identity"]["repository"], sort_keys=True)
            for job in jobs
            if job["state"] == "awaiting_baseline"
        }

    def priority(self, job, blocked=None):
        """The job's effective rank: lower runs first and may preempt higher."""
        blocked = self.blocked(self.jobs()) if blocked is None else blocked
        repository = json.dumps(job["identity"]["repository"], sort_keys=True)
        if job["tier"] == "full" and job["scope"] == "repository" and repository in blocked:
            # Ahead of releases too: waiting pr gates depend on it, so traffic
            # cannot starve it.
            return -1
        return AUDIT_PRIORITY[job["tier"]]

    def runnable(self, now=None):
        now = time.time() if now is None else now
        today = datetime.fromtimestamp(now, UTC).date().isoformat()
        jobs = self.jobs()
        blocked = self.blocked(jobs)
        return sorted(
            (
                job
                for job in jobs
                if job["not_before"] <= now
                and (
                    job["state"] == "queued"
                    or (job["state"] == "deferred" and job.get("deferred_on") != today)
                )
            ),
            key=lambda job: (self.priority(job, blocked), job["sequence"]),
        )

    def outranked(self, rank, now=None):
        """Whether runnable work strictly outranks `rank`; it may preempt that job."""
        blocked = self.blocked(self.jobs())
        return any(self.priority(job, blocked) < rank for job in self.runnable(now))

    def admission_left(self, maximum, now=None):
        now = time.time() if now is None else now
        path = self.usage_path(now)
        used = json.loads(path.read_text(encoding="utf-8"))["attempts"] if path.exists() else 0
        return used < maximum

    def usage_path(self, now):
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        return self.directory.parent / "audit-usage" / (day + ".json")

    def next_job(self, maximum, now=None):
        """Claim the highest-priority runnable request within daily admission."""
        now = time.time() if now is None else now
        today = datetime.fromtimestamp(now, UTC).date().isoformat()
        with self._lock():
            for job in self.runnable(now):
                if job.get("admitted_on") == today:
                    # Resuming a preempted or recovered run admitted today is free.
                    job.update(state="running", started_at=utc_now())
                    self.save(job)
                    return job
                path = self.usage_path(now)
                used = (
                    json.loads(path.read_text(encoding="utf-8"))["attempts"] if path.exists() else 0
                )
                if used >= maximum:
                    if job["tier"] != "full":
                        # Shown in status and retried tomorrow; a gate is never skipped.
                        job.update(
                            state="deferred",
                            deferred_on=datetime.fromtimestamp(now, UTC).date().isoformat(),
                        )
                        self.save(job)
                    continue  # Full audits simply wait for the next day.
                write_receipt(
                    path, {"attempts": used + 1, "maximum": maximum, "updated_at": utc_now()}
                )
                job.update(state="running", started_at=utc_now(), admitted_on=today)
                self.save(job)
                return job
        return None

    def update(self, identifier, *, settling=False, **changes):
        with self._lock():
            job = self.read(identifier)
            if settling and (job["cancel_requested"] or job["superseded_by"]):
                changes = {"state": "cancelled" if job["cancel_requested"] else "superseded"}
            if job.get("schema_version") == 1 and all(
                key in job and job[key] == value for key, value in changes.items()
            ):
                return job  # Polling an unchanged baseline must not create another receipt.
            job.update(changes)
            self.save(job)
            return job

    def stop_reason(self, identifier):
        """Checked by runners at each checkpoint."""
        job = self.read(identifier)
        if job["cancel_requested"]:
            return "cancelled"
        if job["superseded_by"]:
            return "superseded"
        return None

    def retry_publication(self, identifier, reason):
        """Retry only an inspected terminal publication, retaining its completed audit."""
        from cirra import audits

        string(reason, "publication recovery reason", 1000)
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise CirraError("invalid audit job identifier")
        with self._lock():
            path = self.directory / (identifier + ".json")
            source = path if path.is_file() else self.directory / "archive" / path.name
            if not source.is_file():
                raise CirraError("audit job does not exist")
            job = json.loads(source.read_text(encoding="utf-8"))
            if (
                job.get("id") != identifier
                or not re.fullmatch("[a-f0-9]{64}", job.get("work_key", ""))
                or job["state"] != "attention"
                or job["tier"] != "pr"
                or job["cancel_requested"]
                or job["superseded_by"]
                or not job["attempt_id"]
                or job.get("result", {}).get("publication", {}).get("state")
                not in ("failed", "retry")
                or audits.get(self.store, job["attempt_id"])["state"] != "complete"
            ):
                raise CirraError(
                    "only a completed PR audit with a failed publication can be retried"
                )
            if any(
                other["work_key"] == job["work_key"]
                and other["id"] != identifier
                and other["accepted_at"] >= job["accepted_at"]
                for other in self.jobs()
            ):
                raise CirraError("a newer audit request supersedes this publication")
            latest_path = self.directory / "latest" / (job["work_key"] + ".json")
            if latest_path.exists():
                latest = json.loads(latest_path.read_text(encoding="utf-8"))
                if latest["id"] != identifier and latest["accepted_at"] >= job["accepted_at"]:
                    raise CirraError("a newer audit request supersedes this publication")
            # Also check archives made before scope markers existed.
            for earlier in (self.directory / "archive").glob("*.json"):
                other = json.loads(earlier.read_text(encoding="utf-8"))
                if (
                    other["work_key"] == job["work_key"]
                    and other["id"] != identifier
                    and other["accepted_at"] >= job["accepted_at"]
                ):
                    raise CirraError("a newer audit request supersedes this publication")
            job.update(
                state="awaiting_publication",
                not_before=0,
                publication_tries=0,
                recovery_reason=reason,
            )
            self.save(job)
            return job

    def cancel(self, identifier, reason):
        string(reason, "cancellation reason", 1000)
        with self._lock():
            job = self.read(identifier)
            if job["tier"] == "pr" and job["state"] in ACTIVE_AUDIT:
                receipt_path = (
                    self.directory.parent / "audit-publication" / (job["audit_id"] + ".json")
                )
                if receipt_path.exists():
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    if receipt.get("uncertain", receipt.get("state") == "failed") or receipt.get(
                        "state"
                    ) in (
                        "creating",
                        "published",
                    ):
                        raise CirraError(
                            "audit publication already started; reconcile it before cancellation"
                        )
            if job["state"] in ("queued", "deferred"):
                self._mark_attempt(job, "cancelled")  # A preempted job keeps its attempt.
                job.update(state="cancelled", cancel_reason=reason)
            elif job["state"] in (
                "running",
                "awaiting_import",
                "awaiting_baseline",
                "awaiting_publication",
            ):
                fenced = self._mark_attempt(job, "cancelled")
                if job["state"] == "awaiting_import" and fenced == "complete":
                    raise CirraError(
                        "this audit's report was already imported; it settles on the next pass"
                    )
                job.update(cancel_requested=True, cancel_reason=reason)
                if job["state"] != "running":
                    job["state"] = "cancelled"  # No process to stop; the fence is set.
            else:
                raise CirraError(f"audit job is already {job['state']}")
            self.save(job)
            return job

    def status(self):
        keys = (
            "id",
            "tier",
            "scope",
            "reason",
            "state",
            "attempt_id",
            "superseded_by",
            "cancel_requested",
            "accepted_at",
            "updated_at",
            "result",
            "error",
        )
        return [
            {key: job.get(key) for key in keys} | {"head": job["identity"]["head"]}
            for job in self.jobs()
        ]
