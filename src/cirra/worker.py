"""Single-operator worker: fresh provider reads, isolated observation, review, publication."""

import json
import sqlite3
import time
from pathlib import Path

from cirra import gitlab, provenance
from cirra.audit_worker import (
    AuditCheckPublisher,
    AuditPublication,
    AuditRunner,
    audit_once,
    default_branch_head,
    request_audits,
)
from cirra.coordinator import review
from cirra.domain import CirraError, digest, utc_now
from cirra.fixture import FixtureProvider
from cirra.github import GitHubClient, GitHubRateLimited, export_capture, native_id, record
from cirra.job_queue import AuditQueue, Queue
from cirra.locking import change_lock
from cirra.observer import NAME, ObserverCheck, load_observer, observe
from cirra.publish import (
    GitHubPublisher,
    GitLabPublisher,
    github_app_token,
    publish,
    verify_gitlab_permission_receipt,
    write_receipt,
)
from cirra.report import export_report
from cirra.store import Store

# Full-audit schedule checks read the default branch head, so they run once an
# hour; `schedule_days` and `max_age_days` decide whether a check queues work.
AUDIT_SCHEDULE_SECONDS = 3600


def is_open(route, change, reader):
    if route.provider == "github":
        client = GitHubClient(route.repository, token=reader)
        repo, _ = client.get()
        if native_id(record(repo).get("id")) != route.repository_id:
            raise CirraError("worker repository identity changed")
        item, _ = client.get(f"pulls/{change}")
        if (
            record(item).get("number") != change
            or native_id(record(record(item.get("base")).get("repo")).get("id"))
            != route.repository_id
        ):
            raise CirraError("worker received a different pull request identity")
        return item.get("state") == "open" and item.get("draft") is False
    client = gitlab.GitLabClient(route.repository, token=reader)
    project, _ = client.get()
    if native_id(record(project).get("id")) != route.repository_id:
        raise CirraError("worker project identity changed")
    item, _ = client.get(f"merge_requests/{change}")
    if record(item).get("iid") != change or str(item.get("project_id")) != route.repository_id:
        raise CirraError("worker received a different merge request identity")
    return (
        item.get("state") == "opened" and item.get("draft", item.get("work_in_progress")) is False
    )


class Processor:
    def __init__(self, directory, credential, capture):
        self.directory, self.credential, self.capture = (
            Path(directory).resolve(),
            credential,
            capture,
        )

    def app_token(self, route):
        settings = route.publisher
        return github_app_token(
            settings["app_id"],
            settings["installation_id"],
            Path(settings["private_key_file"]).read_text(encoding="utf-8"),
            route.repository_id,
        )

    def __call__(self, route, hint):
        self.audit_requests = []
        result = self.process(route, hint)
        if self.audit_requests:
            result["audits"] = self.audit_requests
        return result

    def request_audits(self, route, change, data, reader):
        """Queue production audits for a captured change; never fails the review job."""
        if getattr(route.policy, "audit", None) is None:
            return []
        files = data["files"]
        paths = sorted(
            {item["path"] for item in files}
            | {item["old_path"] for item in files if item.get("old_path")}
        )
        try:
            return request_audits(
                self.directory,
                route,
                {"kind": "change", "changed_paths": paths},
                head=data["change"]["head_sha"],
                base=data["change"]["diff_base_sha"],
                change=change,
                default_head=lambda: default_branch_head(route, reader),
            )
        except CirraError as exc:
            return [{"error": str(exc)}]

    def schedule_audits(self, routes, now=None):
        """Hourly full-audit schedule checks, starting with the first pass."""
        now = time.monotonic() if now is None else now
        schedule = self.__dict__.setdefault("audit_schedule", {})
        receipts = []
        for name, route in routes.items():
            due = schedule.get(name)
            if due is not None and now < due:
                continue
            schedule[name] = now + AUDIT_SCHEDULE_SECONDS  # Also paces failing reads.
            reader = self.credential(route.reader_env) if route.reader_env else None
            receipts += request_audits(
                self.directory,
                route,
                # Every pass is a schedule check, so restarts cannot postpone a due audit.
                {"kind": "schedule"},
                default_head=lambda route=route, reader=reader: default_branch_head(route, reader),
            )
        return receipts

    def audit(self, config):
        """Run one production-audit pass over routes whose policy has an audit block."""
        routes = {
            name: route
            for name, route in config.routes.items()
            if getattr(route.policy, "audit", None) is not None
        }
        if not routes:
            return None
        self.schedule_audits(routes)
        store = Store(self.directory)
        runner = AuditRunner(
            self.directory,
            routes,
            self.credential,
            heartbeat=lambda: heartbeat(Queue(self.directory), audit_running=True),
        )
        publication = AuditPublication(
            self.directory,
            routes,
            lambda route: AuditCheckPublisher(
                route.repository, route.publisher["app_id"], self.app_token(route)
            ),
        )
        job = audit_once(
            AuditQueue(self.directory, store),
            store,
            min(route.policy.audit.max_audits_per_day for route in routes.values()),
            runner,
            on_complete=publication,
        )
        runner.cleanup(store)
        return job

    def process(self, route, hint):
        reader = self.credential(route.reader_env)
        change = hint["change"]
        branch = hint.get("branch")
        if branch:
            from cirra.reconcile import capture_branch

            data = capture_branch(route, branch, reader)
        else:
            if not is_open(route, change, reader):
                return {"state": "skipped", "reason": "change is closed or a draft"}
            data = self.capture(
                route.provider, route.repository, change, route.policy, token=reader
            )
            self.audit_requests = self.request_audits(route, change, data, reader)
        if data["change"]["repository_id"] != route.repository_id:
            raise CirraError("captured repository differs from the configured worker route")
        receipt_path, witness_key = None, None
        ci_result = None
        if route.observer:
            policy = load_observer(route.observer)
            expected = {
                "kind": "check_run" if route.provider == "github" else "observer_run",
                "source_id": str(route.publisher["app_id"])
                if route.provider == "github"
                else "cirra/docker",
                "name": NAME,
                "definition_digest": policy["definition_digest"],
            }
            if expected not in route.policy.expected_checks:
                raise CirraError(
                    "review policy must pin the observer source, check name, and definition digest"
                )
            witness_key = self.credential(route.witness_env)

            def record_execution(path):
                nonlocal ci_result
                statement = json.loads(path.read_text())["statement"]
                ci_result = {
                    "route": route.name,
                    "repository": route.repository,
                    "change": change,
                    "head_sha": data["change"]["head_sha"],
                    "conclusion": statement["checks"][0]["check"]["raw_state"],
                    "receipt_path": str(path),
                    "observed_at": utc_now(),
                    "publication": "pending" if route.provider == "github" else "local",
                    "branch": branch,
                }
                write_receipt(
                    self.directory / "ci" / f"{route.name}-{digest(change)}.json", ci_result
                )

            receipt_path = observe(
                data,
                policy,
                self.directory,
                witness_key,
                ObserverCheck(
                    route.repository, route.publisher["app_id"], lambda: self.app_token(route)
                )
                if route.provider == "github"
                else None,
                token=reader,
                on_execution=record_execution,
            )
            if ci_result and route.provider == "github":
                ci_result["publication"] = "published"
                write_receipt(
                    self.directory / "ci" / f"{route.name}-{digest(change)}.json", ci_result
                )
            if branch or not getattr(route, "review_enabled", True):
                return {"state": "done", "ci": ci_result, "review": "disabled"}
            # Re-read native CI and the snapshot. GitHub receipts attest a native
            # check; GitLab receipts add explicitly separate observer evidence.
            refreshed = self.capture(
                route.provider,
                route.repository,
                change,
                route.policy,
                token=reader,
                witness=receipt_path,
                witness_key=witness_key,
            )
            if refreshed["change"] != data["change"]:
                raise CirraError("change moved during observation; refresh and retry")
            data = refreshed
        export_capture(data, self.directory)
        provider = FixtureProvider.from_data(data, route.policy)
        store = Store(self.directory)
        report, reused = review(provider, route.policy, store)
        export_report(report, self.directory)
        if report["review"]["outcome"] != "succeeded":
            return {
                "state": "attention",
                "run_id": report["run_id"],
                "reason": "review did not succeed; inspect the report before an explicit rereview",
                "ci": ci_result,
            }
        # A PR can close while tests/review run. It must still be open before a write.
        if not is_open(route, change, reader):
            return {
                "state": "skipped",
                "run_id": report["run_id"],
                "reason": "change closed during review",
            }
        if route.provider == "github":
            publisher = GitHubPublisher(
                route.repository, route.publisher["app_id"], self.app_token(route)
            )
        else:
            token = self.credential(route.publisher["token_env"])
            if not token or token == reader:
                raise CirraError("GitLab worker requires a separate publisher token")
            verify_gitlab_permission_receipt(
                route.publisher["permission_receipt"],
                token,
                route.repository_id,
                route.publisher["principal_id"],
            )
            publisher = GitLabPublisher(
                gitlab.GitLabClient(route.repository, token=token), route.publisher["principal_id"]
            )

        def refresh():
            latest = self.capture(
                route.provider, route.repository, change, route.policy, token=reader
            )
            if receipt_path:
                latest = provenance.apply(latest, receipt_path, witness_key)
            return FixtureProvider.from_data(latest, route.policy)

        receipt = publish(store, route.policy, report["run_id"], refresh, publisher)
        return {
            "state": "done" if receipt["state"] == "published" else "attention",
            "run_id": report["run_id"],
            "reused": reused,
            "publication": receipt["state"],
            "native_id": receipt.get("native_id"),
            "ci": ci_result,
        }


def heartbeat(queue, **extra):
    # Liveness is the file's content and mtime; a history copy per poll would add
    # an fsync'd file every pass.
    write_receipt(
        queue.directory.parent / "worker-status.json",
        {"state": "running", "heartbeat_at": utc_now(), "daily_usage": queue.usage(), **extra},
        history=False,
    )


def work_once(config, queue, process):
    jobs = queue.pending()
    if not jobs:
        return None
    job = jobs[0]
    route = config.routes.get(job["route"])
    if route is None or route.digest != job["route_digest"]:
        job.update(state="attention", error="route or policy changed; inspect before requeuing")
    elif job["attempts"] >= config.max_attempts:
        job.update(state="attention", error="retry limit reached; inspect the saved job")
    else:
        if not queue.reserve_attempt(config.max_jobs_per_day):
            return None  # Keep accepted jobs durable until the next UTC day.
        job.update(state="running", attempts=job["attempts"] + 1, updated_at=utc_now())
        queue.save(job)
        try:
            result = process(route, job["hint"])
            job.update(state=result["state"], result=result)
            job.pop("error", None)
        except GitHubRateLimited as exc:
            # Quota, not failure: wait for GitHub's reset and do not count the
            # attempt, so a limited job never strands in `attention`.
            job.update(
                state="retry",
                attempts=job["attempts"] - 1,
                not_before=max(exc.retry_at, time.time() + config.retry_seconds),
                error=str(exc),
            )
        except (CirraError, OSError, ValueError) as exc:
            job.update(
                state="retry" if job["attempts"] < config.max_attempts else "attention",
                not_before=time.time()
                + min(
                    config.retry_seconds * 2 ** (job["attempts"] - 1),
                    getattr(config, "max_retry_seconds", 3600),
                ),
                error=str(exc) if isinstance(exc, CirraError) else "local worker I/O failed",
            )
    job["updated_at"] = utc_now()
    queue.save(job)
    return job


def run_worker(config, directory, process, *, once=False, stopped=None):
    import threading

    stopped = stopped or threading.Event()
    queue = Queue(directory, config.max_jobs)
    # The OS releases this lock after process death. An abandoned running job
    # resumes through the existing review/publisher recovery mechanisms.
    with change_lock(queue.directory / "locks", "worker", 0):
        next_reconcile = 0
        while not stopped.is_set():
            interval = getattr(config, "reconcile_seconds", 0)
            if interval and time.monotonic() >= next_reconcile:
                from cirra.reconcile import sync

                results = sync(config, queue, process.credential)
                write_receipt(
                    queue.directory.parent / "reconciliation.json",
                    {"observed_at": utc_now(), "routes": results},
                    history=False,
                )
                next_reconcile = time.monotonic() + interval
            heartbeat(queue)
            job = work_once(config, queue, process)
            if job:
                print(
                    json.dumps({key: job.get(key) for key in ("id", "state", "result", "error")}),
                    flush=True,
                )
            audit_job = None
            if getattr(process, "audit", None):
                try:
                    audit_job = process.audit(config)
                except (CirraError, OSError, sqlite3.Error) as exc:
                    # Audits never stop review work; the failure is visible in the log.
                    message = str(exc) if isinstance(exc, CirraError) else type(exc).__name__
                    print(json.dumps({"audit_error": message}), flush=True)
                if audit_job:
                    print(
                        json.dumps(
                            {
                                "audit": {
                                    key: audit_job.get(key)
                                    for key in ("id", "tier", "state", "result", "error")
                                }
                            }
                        ),
                        flush=True,
                    )
            if once:
                return job
            if not job and not audit_job:
                stopped.wait(config.poll_seconds)
