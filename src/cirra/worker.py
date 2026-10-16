"""Single-operator worker: fresh provider reads, isolated observation, review, publication."""

import json
import time
from pathlib import Path

from cirra import gitlab, provenance
from cirra.coordinator import review
from cirra.domain import CirraError, utc_now
from cirra.fixture import FixtureProvider
from cirra.github import GitHubClient, export_capture, native_id, record
from cirra.job_queue import Queue
from cirra.locking import change_lock
from cirra.observer import NAME, ObserverCheck, load_observer, observe
from cirra.publish import (
    GitHubPublisher,
    GitLabPublisher,
    github_app_token,
    publish,
    verify_gitlab_permission_receipt,
)
from cirra.report import export_report
from cirra.store import Store


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
        reader = self.credential(route.reader_env)
        change = hint["change"]
        if not is_open(route, change, reader):
            return {"state": "skipped", "reason": "change is closed or a draft"}
        data = self.capture(route.provider, route.repository, change, route.policy, token=reader)
        if data["change"]["repository_id"] != route.repository_id:
            raise CirraError("captured repository differs from the configured worker route")
        receipt_path, witness_key = None, None
        if route.observer:
            policy = load_observer(route.observer)
            expected = {
                "kind": "check_run",
                "source_id": str(route.publisher["app_id"]),
                "name": NAME,
                "definition_digest": policy["definition_digest"],
            }
            if expected not in route.policy.expected_checks:
                raise CirraError(
                    "review policy must pin the observer App, check name, and definition digest"
                )
            witness_key = self.credential(route.witness_env)
            receipt_path = observe(
                data,
                policy,
                self.directory,
                witness_key,
                ObserverCheck(route.repository, route.publisher["app_id"], self.app_token(route)),
                token=reader,
            )
            # Native CI is re-read; a saved webhook or local execution result is
            # never substituted for the provider's current check identity/state.
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
        }


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
        except (CirraError, OSError, ValueError) as exc:
            job.update(
                state="retry" if job["attempts"] < config.max_attempts else "attention",
                not_before=time.time() + config.retry_seconds,
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
        while not stopped.is_set():
            job = work_once(config, queue, process)
            if job:
                print(
                    json.dumps({key: job.get(key) for key in ("id", "state", "result", "error")}),
                    flush=True,
                )
            if once:
                return job
            if not job:
                stopped.wait(config.poll_seconds)
