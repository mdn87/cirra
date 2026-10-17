"""Worker passes for production-audit requests: request, claim, run, settle, publish.

A runner is `run(job, attempt, stop)` and returns one of
`{"state": "awaiting_import"}` (an operator's session will import the report),
`{"state": "imported", "audit": summary}` (the runner imported a report), or
`{"state": "stopped", "reason": ...}` after `stop()` returned a reason at a
checkpoint. Cancellation and supersession are cooperative: `stop()` reports
them and the attempt's stored state already refuses a late import. A runner
calls `stop(checkpoints=[...])` when the completed domains change; for a full
audit that is where runnable pr or release work preempts it.
"""

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from cirra import audits
from cirra.audit import select_tiers
from cirra.audit_checkout import change_ref, prepare_checkout, remove_checkout
from cirra.dispatch import stop_process, tool_environment
from cirra.domain import CirraError, sha
from cirra.github import API_VERSION, native_id, record, repository_name
from cirra.job_queue import AuditQueue
from cirra.publish import write_receipt
from cirra.store import Store
from cirra.transport import JSONClient, pages

CHECK_NAME = "cirra/audit"
WAITING = ("awaiting_import", "awaiting_baseline")
# A noisy or wedged agent must not fill the state filesystem.
MAX_AGENT_LOG_BYTES = 16 * 1024 * 1024
FINAL = {
    "complete": "done",
    "failed": "failed",
    "cancelled": "cancelled",
    "superseded": "superseded",
}


def settle(queue, store, job, on_complete=None):
    """Move a job whose attempt reached a final state to the matching job state."""
    attempt = audits.get(store, job["attempt_id"])
    if attempt["state"] not in FINAL:
        return job
    result = audits.summary(attempt)
    state = FINAL[attempt["state"]]
    stopped = queue.stop_reason(job["id"])
    if attempt["state"] == "complete" and stopped:
        # Cancelled, or replaced by a newer head, before its result was published.
        return queue.update(job["id"], state=stopped, result=result)
    if attempt["state"] == "complete" and on_complete:
        result["publication"] = on_complete(job, attempt)
        if result["publication"]["state"] == "waiting":
            return queue.update(job["id"], state="awaiting_baseline", result=result)
        if result["publication"]["state"] == "stopped":
            return queue.update(job["id"], state=result["publication"]["reason"], result=result)
        if result["publication"]["state"] == "failed":
            state = "attention"
    return queue.update(job["id"], state=state, result=result)


def recover(queue, store, on_complete=None):
    """Settle waiting jobs and reconcile jobs a stopped worker left `running`.

    The single worker runs one audit at a time inside `audit_once`, so a job
    still `running` when a pass starts was orphaned by a restart: its final
    attempt settles, a pending stop applies, and anything else runs again.
    """
    for job in queue.jobs():
        if job["state"] in WAITING:
            settle(queue, store, job, on_complete)
        elif job["state"] == "running":
            attempt_id = job.get("attempt_id")
            if attempt_id and audits.get(store, attempt_id)["state"] in FINAL:
                settle(queue, store, job, on_complete)
            elif reason := queue.stop_reason(job["id"]):
                if attempt_id:
                    audits.mark(store, attempt_id, reason)
                queue.update(job["id"], state=reason)
            else:
                queue.update(job["id"], state="queued", recovered=job.get("recovered", 0) + 1)


def fail(queue, store, job, attempt_id, error, on_complete=None):
    """End a job whose runner failed, unless a stop or an import already decided it."""
    reason = queue.stop_reason(job["id"])
    if attempt_id:
        if not reason and audits.get(store, attempt_id)["state"] == "complete":
            return settle(queue, store, queue.read(job["id"]), on_complete)
        audits.mark(store, attempt_id, reason or "cancelled")
    if reason:
        return queue.update(job["id"], state=reason)
    return queue.update(job["id"], state="failed", error=error)


def audit_once(queue, store, maximum, run, *, on_complete=None, now=None):
    """Recover and settle earlier work, then run at most one runnable request."""
    recover(queue, store, on_complete)
    job = queue.next_job(maximum, now)
    if job is None:
        return None
    job_id, attempt_id = job["id"], None

    def stop(checkpoints=None):
        reason = queue.stop_reason(job_id)
        if reason is None and checkpoints is not None:
            queue.update(job_id, checkpoints=checkpoints)
            if (
                job["tier"] == "full"
                # Once every domain is done, only the report remains: let it finish.
                and len(checkpoints) < len(audits.catalog_domains())
                and queue.priority_waiting(now)
                and queue.admission_left(maximum, now)
            ):
                return "preempted"
        return reason

    try:
        attempt, _ = audits.begin(
            store, job["identity"], job["scope"], fresh=job.get("fresh", False)
        )
        attempt_id = attempt["attempt_id"]
        job = queue.update(job_id, attempt_id=attempt_id)
        if attempt["state"] == "complete":
            return settle(queue, store, job, on_complete)  # Attached to an existing result.
        if reason := queue.stop_reason(job_id):
            # Cancelled or superseded before the attempt existed to be fenced.
            audits.mark(store, attempt_id, reason)
            return queue.update(job_id, state=reason)
        outcome = run(job, attempt, stop)
        if outcome["state"] == "awaiting_import":
            return queue.update(
                job_id, state="awaiting_import", packet_path=outcome.get("packet_path")
            )
        if outcome["state"] == "stopped" and outcome["reason"] in ("cancelled", "superseded"):
            audits.mark(store, attempt_id, outcome["reason"])
            return queue.update(job_id, state=outcome["reason"])
        if outcome["state"] == "stopped" and outcome["reason"] == "preempted":
            # The attempt stays prepared, so the next run resumes its checkpoints.
            current = queue.read(job_id)
            return queue.update(
                job_id, state="queued", preemptions=current.get("preemptions", 0) + 1
            )
        if outcome["state"] == "imported":
            return settle(queue, store, queue.read(job_id), on_complete)
        raise CirraError(f"audit runner returned an unsupported outcome {outcome!r}")
    except Exception as exc:  # noqa: BLE001 - any runner failure must end and fence the job
        message = str(exc) if isinstance(exc, CirraError) else f"{type(exc).__name__}: {exc}"
        return fail(queue, store, job, attempt_id, message, on_complete)


def route_for(routes, name, identity):
    """The named route, else one serving this repository with the identity's policy."""
    if routes.get(name):
        return routes[name]
    serving = [route for route in routes.values() if repository_of(route) == identity["repository"]]
    matching = [
        route
        for route in serving
        if audits.policy_digest(route.policy.audit) == identity["policy_digest"]
    ]
    return (matching or serving or [None])[0]


def repository_of(route):
    return {
        "provider": route.provider,
        "instance": route.instance,
        "repository_id": route.repository_id,
    }


def default_branch_head(route, reader):
    """The default branch name and its head commit, read from the provider."""
    if route.provider == "github":
        from cirra.github import GitHubClient

        client = GitHubClient(route.repository, token=reader)
        repo, _ = client.get()
        branch = record(repo).get("default_branch")
        item, _ = client.get("branches/" + quote(str(branch), safe=""))
        return branch, sha(record(record(item).get("commit")).get("sha"))
    from cirra.gitlab import GitLabClient

    client = GitLabClient(route.repository, token=reader)
    project, _ = client.get()
    branch = record(project).get("default_branch")
    item, _ = client.get("repository/branches/" + quote(str(branch), safe=""))
    return branch, sha(record(record(item).get("commit")).get("id"))


def request_audits(
    directory, route, event, *, head=None, base=None, change=None, default_head=None
):
    """Queue the tiers `select_tiers` picks for one event. Returns the queue receipts.

    `head`/`base`/`change` describe the change for `change` events. Full audits
    for a baseline, catalog change or age run on the default branch head from
    `default_head()`; a structural full audit runs on the change head, scoped to
    the change, so it never becomes the baseline.
    """
    policy = route.policy.audit
    if policy is None:
        return []
    store = Store(directory)
    queue = AuditQueue(directory, store)
    repository = repository_of(route)
    latest = audits.history(audits.latest_full(store, audits.repository_key(repository)))
    selected = select_tiers(
        policy,
        event,
        latest,
        catalog_digest=audits.vendored_identity()["catalog_digest"],
        now=datetime.now(UTC),
    )
    receipts = []
    for tier, reason in selected:
        context = {"route": route.name, "change": change}
        if tier == "pr" or (tier == "full" and reason == "structural"):
            identity = audits.audit_identity(
                policy, tier, repository, head, base if tier == "pr" else None
            )
            scope = f"change:{change}"
            context["ref"] = change_ref(route.provider, change)
        elif tier == "full":
            branch, default = default_head()
            identity = audits.audit_identity(policy, "full", repository, default)
            scope = "repository"
            context["ref"] = "refs/heads/" + branch
        else:
            continue  # Release audits are requested by release planning.
        receipt = queue.request(
            identity,
            scope,
            reason=reason,
            debounce_seconds=policy.debounce_seconds,
            context=context,
            # Age-triggered re-audits of an unchanged head need a new attempt.
            fresh=reason in ("schedule", "stale"),
        )
        receipts.append({"tier": tier, "reason": reason, **receipt})
    return receipts


class AuditRunner:
    """Prepare a checkout and packet; a session runner stops there, a command runs."""

    def __init__(
        self,
        directory,
        routes,
        credential,
        *,
        url_for=None,
        protocols=("https",),
        poll_seconds=2,
        grace_seconds=30,
        heartbeat=None,
    ):
        self.directory = Path(directory).resolve()
        self.routes, self.credential = routes, credential
        self.url_for, self.protocols = url_for, protocols
        self.poll_seconds, self.grace_seconds = poll_seconds, grace_seconds
        self.heartbeat = heartbeat  # Keeps worker liveness current during long audits.

    def attempt_dir(self, attempt_id):
        return self.directory / "audit-attempts" / attempt_id

    def __call__(self, job, attempt, stop):
        route = route_for(self.routes, job["context"].get("route"), job["identity"])
        if route is None:
            raise CirraError("no route with an audit policy serves this repository")
        policy = route.policy.audit
        identity = job["identity"]
        if identity["policy_digest"] != audits.policy_digest(policy):
            raise CirraError("the audit policy changed after this request; request it again")
        if not job["context"].get("ref"):
            raise CirraError("the audit request names no ref to fetch")
        root = self.attempt_dir(attempt["attempt_id"])
        checkout = root / "checkout"
        if not checkout.exists():
            prepare_checkout(
                self.directory,
                route.provider,
                route.repository,
                job["context"]["ref"],
                identity["head"],
                checkout,
                base=identity["base"],
                token=self.credential(route.reader_env) if route.reader_env else None,
                url=self.url_for(route) if self.url_for else None,
                protocols=self.protocols,
            )
        store = Store(self.directory)
        packet_path = audits.export_packet(
            audits.make_packet(store, attempt, checkout), self.directory
        )
        if policy.runner_kind == "session":
            return {"state": "awaiting_import", "packet_path": packet_path}
        work = root / "work"
        (work / "checkpoints").mkdir(parents=True, exist_ok=True)
        # A preempted run leaves its stop file and possibly a partial report.
        (work / "stop").unlink(missing_ok=True)
        for stale in work.glob("production-audit-*"):
            stale.unlink()
        if identity["tier"] == "full":
            self.resume(store, job, checkout, work / "checkpoints")
        return self.run_agent(policy, identity, attempt, packet_path, checkout, work, stop)

    def resume(self, store, job, checkout, target):
        """Copy a superseded attempt's checkpoints whose evidence files did not change."""
        source_id = job.get("resume_from")
        if not source_id or any(target.glob("*.json")):
            return []
        source = self.attempt_dir(source_id) / "work" / "checkpoints"
        if not source.is_dir():
            return []
        old = json.loads(audits.get(store, source_id)["identity_json"])
        if any(old[key] != job["identity"][key] for key in ("skill_version", "catalog_digest")):
            return []  # Conclusions under another catalog are not reusable.
        old_head = old["head"]
        listing = audits.git(
            checkout, "diff", "--name-only", "--no-renames", old_head, job["identity"]["head"]
        )
        if listing is None:
            return []  # The old head is unavailable: rerun every domain.
        changed = set(listing.splitlines())
        copied = []
        domains = audits.catalog_domains()
        for path in sorted(source.glob("*.json")):
            data = audits.valid_checkpoint(path, domains)
            if data is None:
                continue
            # A row without evidence (not applicable, or missing) rests on what is
            # absent, which a new head may change; only fully cited domains carry over.
            if not data["coverage"] or any(not row["evidence"] for row in data["coverage"]):
                continue
            cited = {item["path"] for row in data["coverage"] for item in row["evidence"]}
            cited |= {finding["path"] for finding in data.get("findings", [])}
            if not cited & changed:
                (target / path.name).write_bytes(path.read_bytes())
                copied.append(path.stem)
        return copied

    def checkpoints(self, work):
        domains = audits.catalog_domains()
        return sorted(
            path.stem
            for path in (work / "checkpoints").glob("*.json")
            if audits.valid_checkpoint(path, domains) is not None
        )

    def launch(self, argv, work, env, log):
        """Start the agent after any agent a crashed worker left in this directory is gone.

        On POSIX the agent inherits a lock on `agent.lock`, so the lock stays held for
        as long as it (or anything it started) runs, even after the worker dies.
        Windows has no inherited lock; there a surviving agent is not detected.
        """
        if os.name == "nt":
            return subprocess.Popen(
                argv,
                cwd=work,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        import fcntl
        import signal

        with (work / "agent.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                previous = int((work / "agent.pid").read_text(encoding="utf-8"))
                try:
                    os.killpg(previous, signal.SIGKILL)  # its own session: pgid == pid
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + self.grace_seconds
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise CirraError("a previous audit agent is still running") from None
                        time.sleep(0.1)
            process = subprocess.Popen(
                argv,
                cwd=work,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(lock.fileno(),),
            )
            (work / "agent.pid").write_text(str(process.pid), encoding="utf-8")
        return process

    def run_agent(self, policy, identity, attempt, packet_path, checkout, work, stop):
        env = tool_environment()
        env.update(
            CIRRA_AUDIT_PACKET=str(packet_path),
            CIRRA_AUDIT_DIR=str(work),
            CIRRA_AUDIT_CHECKOUT=str(checkout),
        )
        log_path = work / "agent.log"
        with log_path.open("ab") as log:
            process = self.launch(list(policy.runner_command), work, env, log)
            deadline = time.monotonic() + policy.audit_timeout_seconds
            completed = self.checkpoints(work)
            try:
                while process.poll() is None:
                    current = self.checkpoints(work)
                    if current != completed:
                        completed = current
                        reason = stop(checkpoints=current)
                    else:
                        reason = stop()
                    if reason:
                        # Checkpoint protocol: the agent checks for `stop` between domains.
                        (work / "stop").write_text(reason, encoding="utf-8")
                        try:
                            process.wait(self.grace_seconds)
                        except subprocess.TimeoutExpired:
                            pass
                        return {"state": "stopped", "reason": reason}
                    if time.monotonic() >= deadline:
                        raise CirraError("audit agent exceeded audit_timeout_seconds")
                    if log_path.stat().st_size > MAX_AGENT_LOG_BYTES:
                        raise CirraError("audit agent output exceeded 16 MiB")
                    if self.heartbeat:
                        self.heartbeat()
                    time.sleep(self.poll_seconds)
            finally:
                if process.poll() is None:
                    stop_process(process)
        if log_path.stat().st_size > MAX_AGENT_LOG_BYTES:
            raise CirraError("audit agent output exceeded 16 MiB")  # a fast exit skipped polling
        if process.returncode:
            raise CirraError(f"audit agent exited with status {process.returncode}; see {log_path}")
        reports = sorted(work.glob(f"production-audit-{identity['tier']}-*.json"))
        if len(reports) != 1:
            raise CirraError("audit agent must leave exactly one report in its work directory")
        summary, _ = audits.import_report(
            Store(self.directory), attempt["attempt_id"], reports[0], checkout
        )
        return {"state": "imported", "audit": summary}

    def cleanup(self, store):
        """Remove worktrees of attempts that reached a final state."""
        root = self.directory / "audit-attempts"
        for directory in root.glob("*/checkout") if root.exists() else ():
            try:
                attempt = audits.get(store, directory.parent.name)
            except CirraError:
                continue
            if attempt["state"] not in FINAL:
                continue
            identity = json.loads(attempt["identity_json"])
            for route in self.routes.values():
                if repository_of(route) == identity["repository"]:
                    remove_checkout(self.directory, route.provider, route.repository, directory)
                    break


class AuditCheckPublisher:
    """One `cirra/audit` check run per audit identity on the audited head."""

    def __init__(self, repository, app_id, token, *, http=None):
        self.app_id = str(app_id)
        self.prefix = "/repos/" + repository_name(repository)
        self.http = http or JSONClient(
            "https://api.github.com",
            {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": API_VERSION},
        )

    def publish(self, head, external_id, conclusion, summary):
        def get(route, params):
            data, more = self.http.request("GET", self.prefix + "/" + route, params)
            return record(data).get("check_runs"), more

        found = [
            item
            for item in pages(
                get,
                f"commits/{head}/check-runs",
                params={"filter": "all", "check_name": CHECK_NAME},
            )
            if item.get("head_sha") == head
            and item.get("name") == CHECK_NAME
            and item.get("external_id") == external_id
            and str(record(item.get("app")).get("id")) == self.app_id
        ]
        if found:
            return {"native_id": native_id(found[0].get("id")), "existing": True}
        item, _ = self.http.request(
            "POST",
            self.prefix + "/check-runs",
            body={
                "name": CHECK_NAME,
                "head_sha": head,
                "status": "completed",
                "conclusion": conclusion,
                "external_id": external_id,
                "output": {"title": "Cirra production audit", "summary": summary},
            },
        )
        if (
            record(item).get("external_id") != external_id
            or str(record(item.get("app")).get("id")) != self.app_id
        ):
            raise CirraError("GitHub check run acknowledgement failed ownership validation")
        return {"native_id": native_id(item.get("id")), "existing": False}


class AuditPublication:
    """Publish completed pr audits once per identity; other tiers gate elsewhere."""

    def __init__(self, directory, routes, publisher_for):
        self.directory = Path(directory).resolve()
        self.routes, self.publisher_for = routes, publisher_for

    def __call__(self, job, attempt):
        if attempt["tier"] != "pr":
            return {"state": "not_published", "reason": f"{attempt['tier']} audits gate elsewhere"}
        path = self.directory / "audit-publication" / f"{attempt['audit_id']}.json"
        if path.exists():
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if receipt["state"] == "published":
                return {**receipt, "duplicate": True}
        identity = json.loads(attempt["identity_json"])
        route = route_for(self.routes, job["context"].get("route"), identity)
        if route is None:
            return {"state": "failed", "reason": "no route with an audit policy serves this audit"}
        policy = route.policy.audit
        if identity["policy_digest"] != audits.policy_digest(policy):
            return {
                "state": "failed",
                "reason": "the audit policy changed after this request; request it again",
            }
        comparison = None
        if "full" in policy.tiers:
            # Findings are only new against a baseline; without one, the gate waits.
            baseline = audits.latest_full(Store(self.directory), attempt["repository_key"])
            if baseline is None:
                return {"state": "waiting", "reason": "waiting for the first full audit baseline"}
            comparison = audits.compare(json.loads(attempt["report_json"]), baseline)
        result = audits.gate(policy, "pr", attempt, comparison)
        conclusion = (
            "neutral"
            if result["level"] == "none"
            else {"block": "failure", "pass": "success"}[result["state"]]
        )
        receipt = {
            "state": "published",
            "audit_id": attempt["audit_id"],
            "attempt_id": attempt["attempt_id"],
            "gate": result["state"],
            "conclusion": conclusion,
            "blocking": len(result["blocking"]),
        }
        if reason := AuditQueue(self.directory).stop_reason(job["id"]):
            # Cancelled or superseded while waiting: never publish stopped work.
            return {"state": "stopped", "reason": reason}
        if route.provider != "github":
            receipt.update(
                state="unsupported", reason="audit publication is implemented for GitHub only"
            )
        else:
            try:
                receipt.update(
                    self.publisher_for(route).publish(
                        attempt["head"],
                        attempt["audit_id"],
                        conclusion,
                        audits.render(attempt, result),
                    )
                )
            except CirraError as exc:
                receipt.update(state="failed", reason=str(exc))
        write_receipt(path, receipt)
        return receipt
