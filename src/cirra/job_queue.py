"""Atomic file-backed deliveries; one process-lifetime worker lock, no DB migration."""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from cirra.domain import CirraError, digest, utc_now
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
