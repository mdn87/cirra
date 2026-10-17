"""Atomic file-backed deliveries; one process-lifetime worker lock, no DB migration."""

import json
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from cirra.domain import CirraError, digest, number, string, utc_now
from cirra.locking import change_lock
from cirra.publish import write_receipt


class Queue:
    def __init__(self, directory, maximum=10000):
        self.directory = Path(directory).resolve() / "queue"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.maximum = maximum

    def read(self, path):
        raw = path.read_bytes()
        if len(raw) > 65536:
            raise CirraError("queue record exceeds byte limit")
        return json.loads(raw)

    def save(self, job):
        write_receipt(self.directory / (job["id"] + ".json"), job)

    def enqueue(self, route, hint):
        identifier = digest([route.name, hint["payload_digest"]])
        delivery = digest([route.name, hint["delivery"]])
        with change_lock(self.directory / "locks", "enqueue", 2):
            delivery_path = self.directory / "deliveries" / (delivery + ".json")
            if delivery_path.exists() and self.read(delivery_path)["job_id"] != identifier:
                raise CirraError("webhook delivery identity was reused with another payload")
            path = self.directory / (identifier + ".json")
            if path.exists():
                return {"job_id": identifier, "duplicate": True}
            if sum(1 for _ in self.directory.glob("*.json")) >= self.maximum:
                raise CirraError(
                    "queue capacity reached; archive completed deliveries before retrying"
                )
            job = {
                "id": identifier,
                "route": route.name,
                "route_digest": route.digest,
                "hint": hint,
                "state": "queued",
                "attempts": 0,
                "not_before": 0,
                "accepted_at": utc_now(),
                "updated_at": utc_now(),
            }
            # Record delivery identity first: a crash can be retried without losing the job.
            write_receipt(delivery_path, {"job_id": identifier})
            self.save(job)
            return {"job_id": identifier, "duplicate": False}

    def scan(self):
        """Readable job records, and names of records that could not be read."""
        jobs, unreadable = [], []
        for path in self.directory.glob("*.json"):
            try:
                jobs.append(self.read(path))
            except (OSError, ValueError, CirraError):
                unreadable.append(path.name)
        return jobs, sorted(unreadable)

    def pending(self):
        # An unreadable record is skipped (and reported by `cirra health`)
        # rather than crashing the worker loop on every pass.
        jobs, _ = self.scan()
        return sorted(
            (
                job
                for job in jobs
                if job["state"] in ("queued", "running", "retry")
                and job["not_before"] <= time.time()
            ),
            key=lambda job: (job["accepted_at"], job["id"]),
        )

    def tested_head(self, route, change, head):
        """Whether a finished job on this route already tested `head` for `change`."""
        for job in self.scan()[0]:
            ci = (job.get("result") or {}).get("ci") or {}
            if (
                job.get("route") == route.name
                and job.get("state") == "done"
                and ci.get("change") == change
                and ci.get("head_sha") == head
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
            if not path.is_file():
                raise CirraError("job does not exist")
            job = self.read(path)
            if job["route"] != route.name:
                raise CirraError("job belongs to a different route")
            if job["state"] not in ("done", "attention", "skipped"):
                raise CirraError("only terminal jobs may be requeued")
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

    def reserve_attempt(self, maximum):
        # The caller holds the process-lifetime worker lock. Persist before work
        # so a crash cannot reset the daily admission limit.
        usage = self.usage()
        if usage["attempts"] >= maximum:
            return False
        write_receipt(
            self.usage_path(),
            {"attempts": usage["attempts"] + 1, "maximum": maximum, "updated_at": utc_now()},
        )
        return True


AUDIT_PRIORITY = {"release": 0, "pr": 1, "full": 2}
ACTIVE_AUDIT = ("queued", "deferred", "running", "awaiting_import", "awaiting_baseline")
# Finished records move to audit-queue/archive after this long, so each request
# scans only recent and active work; completed results stay attachable in SQLite.
ARCHIVE_AFTER_SECONDS = 7 * 86400
ARCHIVED_STATES = ("done", "superseded", "cancelled", "failed", "attention")


class AuditQueue:
    """Production-audit requests: one queued and one running request per work key.

    A work key is repository, scope (change, release ref, or the repository for
    full audits) and tier. Newer requests coalesce queued ones and flag running
    ones as superseded; identical identities attach to existing work.
    """

    def __init__(self, directory, store=None, maximum=10000):
        self.directory = Path(directory).resolve() / "audit-queue"
        self.directory.mkdir(parents=True, exist_ok=True)
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
                    and type(job.get("sequence")) is int
                    and isinstance(job.get("id"), str)
                    and job.get("state") in (*ACTIVE_AUDIT, *ARCHIVED_STATES)
                    and isinstance(job.get("identity"), dict)
                ):
                    raise ValueError("audit record lacks required fields")
            except (OSError, ValueError, RecursionError):
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
                target.parent.mkdir(exist_ok=True)
                (self.directory / (job["id"] + ".json")).replace(target)
            else:
                kept.append(job)
        return kept

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
            replaced = {"queued", "deferred"}  # same-key requests the new one supersedes
            active = sum(
                job["state"] in ACTIVE_AUDIT
                and not (job["work_key"] == work_key and job["state"] in replaced)
                for job in jobs
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
                    other["state"] in ("running", "awaiting_import", "awaiting_baseline")
                    and not other["superseded_by"]
                ):
                    # The runner stops at its next checkpoint; the attempt's state is
                    # the fence that refuses a late import of the old head.
                    other["superseded_by"] = job["id"]
                    self._mark_attempt(other, "superseded")
                    self.save(other)
            self.save(job)
            return {"job_id": job["id"], "attached": False}

    def runnable(self, now=None):
        now = time.time() if now is None else now
        today = datetime.fromtimestamp(now, UTC).date().isoformat()
        jobs = self.jobs()
        # Repositories whose finished pr audits wait on a first baseline: that
        # baseline runs ahead of further pr work, so traffic cannot starve it.
        blocked = {
            json.dumps(job["identity"]["repository"], sort_keys=True)
            for job in jobs
            if job["state"] == "awaiting_baseline"
        }

        def priority(job):
            repository = json.dumps(job["identity"]["repository"], sort_keys=True)
            if job["tier"] == "full" and job["scope"] == "repository" and repository in blocked:
                return 0.5
            return AUDIT_PRIORITY[job["tier"]]

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
            key=lambda job: (priority(job), job["sequence"]),
        )

    def priority_waiting(self, now=None):
        """Whether pr or release work is runnable; it may preempt a full audit."""
        return any(job["tier"] != "full" for job in self.runnable(now))

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

    def update(self, identifier, **changes):
        with self._lock():
            job = self.read(identifier)
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

    def cancel(self, identifier, reason):
        string(reason, "cancellation reason", 1000)
        with self._lock():
            job = self.read(identifier)
            if job["state"] in ("queued", "deferred"):
                self._mark_attempt(job, "cancelled")  # A preempted job keeps its attempt.
                job.update(state="cancelled", cancel_reason=reason)
            elif job["state"] in ("running", "awaiting_import", "awaiting_baseline"):
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
