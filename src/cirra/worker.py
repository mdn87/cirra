"""Single-operator worker: fresh provider reads, isolated observation, review, publication."""

import json
import os
import random
import time
import uuid
from contextlib import contextmanager
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
from cirra.domain import CirraError, ProviderAccessDenied, ProviderRateLimited, digest, utc_now
from cirra.fixture import FixtureProvider
from cirra.github import GitHubClient, export_capture, native_id, record
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
from cirra.queue_format import activate
from cirra.report import export_report
from cirra.review_gate import RequiredReviewCheck, merge_revision
from cirra.store import Store

# Full-audit schedule checks read the default branch head, so they run once an
# hour; `schedule_days` and `max_age_days` decide whether a check queues work.
AUDIT_SCHEDULE_SECONDS = 3600


@contextmanager
def using_reader(*, preflight=False):
    try:
        yield
    except (ProviderAccessDenied, ProviderRateLimited) as exc:
        exc.reader_refusal = True
        exc.before_execution = preflight
        raise


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
        pem = self.credential(settings["private_key_env"])
        if not pem:
            raise CirraError("GitHub App private key GLAS binding is missing")
        return github_app_token(
            settings["app_id"],
            settings["installation_id"],
            pem,
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
        except (ProviderAccessDenied, ProviderRateLimited) as exc:
            Queue(self.directory).defer_reader(route, exc)
            return [{"error": str(exc)}]
        except CirraError as exc:
            return [{"error": str(exc)}]

    def schedule_audits(self, routes, now=None):
        """Hourly full-audit schedule checks, starting with the first pass."""
        now = time.monotonic() if now is None else now
        schedule = self.__dict__.setdefault("audit_schedule", {})
        queue = Queue(self.directory)
        receipts = []
        for name, route in routes.items():
            due = schedule.get(name)
            if due is not None and now < due:
                continue
            if queue.cooldown(queue.reader_key(route)) > time.time():
                continue
            schedule[name] = now + AUDIT_SCHEDULE_SECONDS  # Also paces failing reads.
            self.audit_context = {"phase": "schedule", "route": name}
            try:
                reader = self.reader_credential(route)
                receipts += request_audits(
                    self.directory,
                    route,
                    {"kind": "schedule"},
                    default_head=lambda route=route, reader=reader: default_branch_head(
                        route, reader
                    ),
                )
            except (ProviderAccessDenied, ProviderRateLimited) as exc:
                queue.defer_reader(route, exc)
                refusal = {"route": name, "error": str(exc)}
                receipts.append(refusal)
                print(
                    json.dumps(
                        {
                            "operation": "schedule_audit",
                            "schedule_id": uuid.uuid4().hex,
                            "level": "warning",
                            **refusal,
                        }
                    ),
                    flush=True,
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
        self.audit_context = {"phase": "queue"}
        store = Store(self.directory)
        runner = AuditRunner(
            self.directory,
            routes,
            self.credential,
            heartbeat=lambda: heartbeat(Queue(self.directory, config.max_jobs), audit_running=True),
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
            on_job=lambda job: self.audit_context.update(
                job_id=job["id"],
                attempt_id=job.get("attempt_id"),
                route=job["context"].get("route"),
            ),
            # A delivery waiting in the review queue preempts a full audit, so the
            # worker can turn it into pr audit requests.
            interrupt=lambda: bool(Queue(self.directory).pending(config.routes)),
        )
        runner.cleanup(store)
        return job

    def reader_credential(self, route):
        try:
            return self.credential(route.reader_env) if route.reader_env else None
        except CirraError as exc:
            refusal = ProviderAccessDenied("configured reader credential is unavailable")
            refusal.before_execution = True
            raise refusal from exc

    def process(self, route, hint):
        reader = self.reader_credential(route)
        change = hint.get("change")
        branch = hint.get("branch")
        if not branch and (type(change) is not int or change < 1):
            raise CirraError("queued work is missing a valid change number")
        with using_reader(preflight=True):
            if branch:
                from cirra.reconcile import capture_branch

                data = capture_branch(route, branch, reader)
            else:
                if not is_open(route, change, reader):
                    return {"state": "skipped", "reason": "change is closed or a draft"}
                data = self.capture(
                    route.provider, route.repository, change, route.policy, token=reader
                )
        if not branch:
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
            with using_reader():
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
        gate = None
        if (
            route.policy.merge_gate
            and data["collection"]["target_branch"] == route.policy.merge_gate.branch
        ):

            def gate_transport():
                return GitHubPublisher(
                    route.repository, route.publisher["app_id"], self.app_token(route)
                ).http

            def current_merge():
                with using_reader():
                    return merge_revision(
                        GitHubClient(route.repository, token=reader),
                        data["change"],
                        route.policy.merge_gate.branch,
                    )

            gate = RequiredReviewCheck(
                self.directory, route, data["change"], gate_transport, current_merge
            )
            gate.start()
        report, reused = review(provider, route.policy, store)
        export_report(report, self.directory)
        failed_review = report["review"]["outcome"] != "succeeded"
        if failed_review and not gate:
            return {
                "state": "attention",
                "run_id": report["run_id"],
                "reason": "review did not succeed; inspect the report before an explicit rereview",
                "ci": ci_result,
            }
        # A PR can close while tests/review run. It must still be open before a write.
        with using_reader():
            still_open = is_open(route, change, reader)
        if not still_open:
            return {
                "state": "skipped",
                "run_id": report["run_id"],
                "reason": "change closed during review",
            }

        def refresh():
            with using_reader():
                latest = self.capture(
                    route.provider, route.repository, change, route.policy, token=reader
                )
            if receipt_path:
                latest = provenance.apply(latest, receipt_path, witness_key)
            return FixtureProvider.from_data(latest, route.policy)

        if gate:
            receipt = gate.finish(store, report, refresh)
            result = {
                "state": "attention" if failed_review else "done",
                "run_id": report["run_id"],
                "reused": reused,
                "publication": receipt["state"],
                "native_id": receipt["native_id"],
                "review_gate": receipt,
                "ci": ci_result,
            }
            if failed_review:
                result["reason"] = (
                    "review did not succeed; inspect the report before an explicit rereview"
                )
            return result
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
        {
            "state": "running",
            "pid": os.getpid(),
            "max_jobs": queue.maximum,
            "invocation_id": os.environ.get("INVOCATION_ID"),
            "heartbeat_at": utc_now(),
            "daily_usage": queue.usage(),
            **extra,
        },
        history=False,
    )


def work_once(config, queue, process):
    jobs = queue.pending(config.routes)
    if not jobs:
        return None
    job = jobs[0]
    route = config.routes.get(job["route"])
    if route is None or route.digest != job["route_digest"]:
        job.update(state="attention", error="route or policy changed; inspect before requeuing")
    elif job["attempts"] >= config.max_attempts:
        job.update(state="attention", error="retry limit reached; inspect the saved job")
    else:
        reservation = queue.usage_path()
        if not queue.reserve_attempt(config.max_jobs_per_day, reservation):
            return None  # Keep accepted jobs durable until the next UTC day.
        started = time.monotonic()
        job.update(
            state="running",
            attempts=job["attempts"] + 1,
            started_at=utc_now(),
            updated_at=utc_now(),
        )
        queue.save(job)
        try:
            result = process(route, job["hint"])
            job.update(state=result["state"], result=result)
            job.pop("error", None)
        except (ProviderAccessDenied, ProviderRateLimited) as exc:
            limited = isinstance(exc, ProviderRateLimited)
            until = (
                max(exc.retry_at, time.time() + config.retry_seconds)
                if limited
                else time.time() + 900
            )
            preflight = getattr(exc, "before_execution", False)
            queue.defer_route(
                route, until, reader=preflight or getattr(exc, "reader_refusal", False)
            )
            if preflight:
                queue.return_unused_attempt(reservation)
            if limited:
                if preflight:
                    job["attempts"] -= 1
                job.update(
                    state="retry" if job["attempts"] < config.max_attempts else "attention",
                    not_before=until,
                )
            else:
                job.update(state="attention")
            job["error"] = str(exc)
        except Exception as exc:  # A bad delivery cannot stop unrelated work.
            job.update(
                state="retry" if job["attempts"] < config.max_attempts else "attention",
                not_before=time.time()
                + min(
                    config.retry_seconds * 2 ** (job["attempts"] - 1) * random.uniform(1, 1.25),
                    getattr(config, "max_retry_seconds", 3600),
                ),
                error=str(exc)
                if isinstance(exc, CirraError)
                else f"worker processing failed ({type(exc).__name__})",
            )
        job["attempt_seconds"] = round(time.monotonic() - started, 3)
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
        activate(queue.directory.parent)
        next_reconcile = 0
        while not stopped.is_set():
            try:
                ready = queue.maintain()
            except (CirraError, OSError, ValueError) as exc:
                print(
                    json.dumps(
                        {
                            "level": "error",
                            "operation": "maintain_queue",
                            "observed_at": utc_now(),
                            "type": type(exc).__name__,
                        }
                    ),
                    flush=True,
                )
                ready = True  # Keep serving already accepted work while maintenance retries.
            if not ready:
                heartbeat(queue)
                stopped.wait(0.05)
                continue
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
                    json.dumps(
                        {
                            key: job.get(key)
                            for key in (
                                "id",
                                "route",
                                "state",
                                "attempts",
                                "started_at",
                                "updated_at",
                                "attempt_seconds",
                                "result",
                                "error",
                            )
                        }
                        | {
                            "operation": "process_review",
                            "level": {"attention": "error", "retry": "warning"}.get(
                                job["state"], "info"
                            ),
                        }
                    ),
                    flush=True,
                )
            audit_job = None
            if getattr(process, "audit", None):
                pass_id, start = uuid.uuid4().hex, time.monotonic()
                try:
                    audit_job = process.audit(config)
                except Exception as exc:
                    # Audits never stop review work; the failure is visible in the log.
                    message = str(exc) if isinstance(exc, CirraError) else type(exc).__name__
                    print(
                        json.dumps(
                            {
                                "audit_error": message,
                                "level": "error",
                                "operation": "audit_pass",
                                "pass_id": pass_id,
                                "observed_at": utc_now(),
                                "duration_ms": round(1000 * (time.monotonic() - start)),
                                **getattr(process, "audit_context", {}),
                            }
                        ),
                        flush=True,
                    )
                if audit_job:
                    print(
                        json.dumps(
                            {
                                "audit": {
                                    key: audit_job.get(key)
                                    for key in (
                                        "id",
                                        "attempt_id",
                                        "tier",
                                        "state",
                                        "updated_at",
                                        "result",
                                        "error",
                                    )
                                },
                                "route": audit_job["context"].get("route"),
                                "operation": "process_audit",
                                "level": "error"
                                if audit_job["state"] in ("failed", "attention")
                                else "info",
                                "pass_id": pass_id,
                                "duration_ms": round(1000 * (time.monotonic() - start)),
                            }
                        ),
                        flush=True,
                    )
            if once:
                return job
            if not job and not audit_job:
                stopped.wait(config.poll_seconds)
