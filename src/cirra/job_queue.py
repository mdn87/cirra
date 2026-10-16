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

    def pending(self):
        jobs = [self.read(path) for path in self.directory.glob("*.json")]
        return sorted(
            (
                job
                for job in jobs
                if job["state"] in ("queued", "running", "retry")
                and job["not_before"] <= time.time()
            ),
            key=lambda job: (job["accepted_at"], job["id"]),
        )

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
        jobs = [self.read(path) for path in self.directory.glob("*.json")]
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
