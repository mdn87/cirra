"""Operator health check over the service state directory, for a timer or monitor."""

import json
import math
import shutil
import sqlite3
import time
from datetime import UTC, datetime, timedelta

from cirra.audits import repository_key
from cirra.data_authority import HEX, valid_authority
from cirra.domain import CirraError
from cirra.job_queue import AuditQueue, Queue
from cirra.store import Store

# A bounded test run plus review can hold the loop for several minutes, during
# which the heartbeat legitimately does not move.
DEFAULT_HEARTBEAT_AGE = 1200
DEFAULT_ATTENTION_WINDOW = None
DELIVERY_TARGET_SECONDS = 1800
BACKUP_MAX_AGE = 23 * 3600  # Alert before the one-day recovery limit is breached.
BACKUP_INITIAL_GRACE = 3600
AUDIT_BACKLOG_AGE = 48 * 3600


def queue_metrics(queue, *, now=None, scanned=None, history=True):
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=1)
    jobs, unreadable = scanned if scanned is not None else queue.scan()
    status_path = queue.directory.parent / "worker-status.json"
    maximum = queue.maximum
    if status_path.exists():
        try:
            maximum = queue.read(status_path).get("max_jobs", maximum)
            if type(maximum) is not int or maximum < 1:
                maximum = queue.maximum
        except (AttributeError, CirraError, OSError, ValueError):
            pass
    pending = [j for j in jobs if j["state"] in ("queued", "retry", "running")]
    cooldowns = {}
    oldest = max(
        (
            now - _parse(j["accepted_at"])
            for j in pending
            if queue.eligible_at(j, cooldowns) <= now.timestamp()
        ),
        default=timedelta(),
    )
    occupied = len(unreadable) + len(pending) + sum(j["state"] == "attention" for j in jobs)
    refusal = queue.directory.parent / "queue-refusal.json"
    last_refusal = _record(queue, refusal).get("observed_at")
    counts = _record(queue, queue.directory.parent / "webhook-admissions" / f"{now.date()}.json")
    counts = {kind: counts.get(kind, 0) for kind in ("accepted", "duplicate", "refused")}
    attempts = sum(counts.values())
    census_pending = queue.slots.count() is None
    metrics = {
        "observed_at": now.isoformat(),
        "capacity": maximum,
        "remaining": 0 if census_pending else max(0, maximum - occupied),
        "census_pending": census_pending,
        "admissions_today": counts,
        "acceptance_percent_today": round(100 * (attempts - counts["refused"]) / attempts, 2)
        if attempts
        else None,
        "oldest_eligible_seconds": max(0, int(oldest.total_seconds())),
        "unreadable_cooldowns": sum(not math.isfinite(value) for value in cooldowns.values()),
        "last_refusal": last_refusal,
    }
    if not history:
        return metrics
    archived_jobs = []
    for path in (queue.directory / "archive").glob("*.json"):
        try:
            if path.stat().st_mtime >= cutoff.timestamp():
                archived_jobs.append(queue.review_record(path))
        except (OSError, ValueError, OverflowError, RecursionError, CirraError):
            continue
    # A requeued archived job retains its original evidence. Count its active record once.
    unique = {j["id"]: j for j in (*archived_jobs, *jobs)}.values()
    arrivals = completed = eligible = on_time = 0
    for job in unique:
        accepted, updated = _parse(job["accepted_at"]), _parse(job["updated_at"])
        if job["hint"].get("source") or type(job["hint"].get("change")) is not int:
            continue
        if updated >= cutoff and job["state"] == "done":
            completed += 1
        if accepted < cutoff:
            continue
        arrivals += 1
        if job["state"] == "skipped" or (now - accepted).total_seconds() < DELIVERY_TARGET_SECONDS:
            continue
        eligible += 1
        on_time += (
            job["state"] == "done"
            and (updated - accepted).total_seconds() <= DELIVERY_TARGET_SECONDS
        )
    return {
        **metrics,
        "webhook_arrivals_24h": arrivals,
        "webhook_completions_24h": completed,
        "eligible_webhooks_24h": eligible,
        "completed_within_30m": on_time,
        "on_time_percent": round(100 * on_time / eligible, 2) if eligible else None,
    }


def _parse(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _record(queue, path):
    if not path.exists():
        return {}
    record = queue.read(path)
    if not isinstance(record, dict):
        raise CirraError(f"invalid operations record: {path.name}")
    return record


def _capture_time(record):
    try:
        captured = datetime.fromisoformat(record["captured_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return captured if captured.utcoffset() is not None else None


def _backup_problem(queue, status, now):
    root = queue.directory.parent
    if not (root / "coordination-state.json").exists() or status.get("state") != "running":
        return None
    try:
        authority = _record(queue, root / "data-authority.json")
        if not valid_authority(authority):
            raise CirraError("invalid local data authority")
        if authority["target"] is not None:
            return None  # A frozen handoff is checked by the coordination health path.
        backup = _record(queue, root / "data-backup.json")
        captured = _capture_time(backup)
        valid = (
            captured is not None
            and isinstance(backup.get("snapshot_id"), str)
            and HEX.fullmatch(backup["snapshot_id"])
            and all(backup.get(k) == authority[k] for k in ("host", "epoch"))
            and authority["floor"] - 60 <= captured.timestamp()
            and -60 <= (now - captured).total_seconds() < BACKUP_MAX_AGE
        )
        if valid:
            return None
        previous_tenure = any(backup.get(k) != authority[k] for k in ("host", "epoch"))
        if (not backup or previous_tenure) and (
            0 <= now.timestamp() - authority["floor"] < BACKUP_INITIAL_GRACE
        ):
            return None
    except (CirraError, OSError, ValueError, TypeError, OverflowError):
        pass
    return {
        "kind": "backup",
        "detail": "active data host has no verified backup less than 23 hours old",
    }


def _restore_problem(queue, status, now):
    root = queue.directory.parent
    if not (root / "coordination-state.json").exists() or status.get("state") != "standby":
        return None
    try:
        authority = _record(queue, root / "data-authority.json")
        restored = _record(queue, root / "data-restored.json")
        captured = _capture_time(restored)
        if (
            valid_authority(authority)
            and all(restored.get(k) == authority[k] for k in ("host", "epoch", "tip"))
            and isinstance(restored.get("snapshot_id"), str)
            and HEX.fullmatch(restored["snapshot_id"])
            and captured is not None
            and -60 <= (now - captured).total_seconds() < BACKUP_MAX_AGE
        ):
            return None
    except (CirraError, OSError, ValueError, TypeError, OverflowError):
        pass
    return {
        "kind": "restore",
        "detail": "standby has no matching verified restore less than 23 hours old",
    }


def _recovery_metrics(queue, status, now):
    result = {"worker_state": status.get("state"), "max_age_seconds": BACKUP_MAX_AGE}
    for label, filename in (("backup", "data-backup.json"), ("restore", "data-restored.json")):
        captured = None
        try:
            captured = _capture_time(_record(queue, queue.directory.parent / filename))
        except (CirraError, OSError, ValueError, TypeError, OverflowError):
            pass
        result[label] = {
            "captured_at": captured.isoformat() if captured else None,
            "age_seconds": int((now - captured).total_seconds()) if captured else None,
        }
    return result


def _audit_metrics(root, jobs, now, policies):
    """Read-only operational visibility; audit delay never changes availability."""
    warnings = []
    waiting = {}
    for state in ("awaiting_import", "deferred"):
        ages = [
            max(0, int((now - _parse(job["accepted_at"])).total_seconds()))
            for job in jobs
            if job["state"] == state
            and not job.get("superseded_by")
            and not job["cancel_requested"]
        ]
        waiting[state] = {"count": len(ages), "oldest_request_age_seconds": max(ages, default=0)}
        if max(ages, default=0) > AUDIT_BACKLOG_AGE:
            warnings.append(
                {
                    "kind": "audit-backlog",
                    "state": state,
                    "detail": f"{state} audit requests include work older than 48 hours",
                }
            )
    baselines = []
    keys = list(policies or {})
    try:
        if (root / "cirra.sqlite3").exists():
            with Store(root, readonly=True).connection() as connection:
                deadline = time.monotonic() + 2
                connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                if policies is None:
                    keys = [
                        row[0]
                        for row in connection.execute(
                            "SELECT DISTINCT repository_key FROM audits LIMIT 257"
                        )
                    ]
                if len(keys) > 256:
                    raise CirraError("audit health repository limit exceeded")
                for key in keys:
                    row = connection.execute(
                        "SELECT attempt_id, completed_at FROM audits WHERE repository_key=? "
                        "AND tier='full' AND state='complete' AND scope='repository' "
                        "ORDER BY completed_at DESC, rowid DESC LIMIT 1",
                        (key,),
                    ).fetchone()
                    baselines.append(_baseline_age(key, row, now, policies))
        else:
            baselines = [_baseline_age(key, None, now, policies) for key in keys]
        for baseline in baselines:
            if baseline["overdue"]:
                warnings.append(
                    {
                        "kind": "audit-baseline",
                        "repository_key": baseline["repository_key"],
                        "detail": "repository full audit is missing or exceeds its configured age",
                    }
                )
    except (CirraError, sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
        warnings.append({"kind": "audit-baseline", "detail": "cannot read audit baseline ages"})
        baselines = []
    return {
        "backlog_warning_seconds": AUDIT_BACKLOG_AGE,
        "waiting": waiting,
        "baselines": baselines,
        "baseline_policy_known": policies is not None,
    }, warnings


def _baseline_age(key, row, now, policies):
    age = max(0, int(now.timestamp() - row["completed_at"])) if row else None
    maximum = policies.get(key) if policies is not None else None
    return {
        "repository_key": key,
        "attempt_id": row["attempt_id"] if row else None,
        "age_seconds": age,
        "max_age_days": maximum,
        "overdue": (age is None or age > maximum * 86400) if maximum is not None else None,
    }


def _archived_review_failures(queue, current):
    jobs, unreadable = [], []
    present = {job["id"] for job in current}
    for index, path in enumerate((queue.directory / "archive").glob("*.json")):
        if index >= 10000:
            unreadable.append("archive exceeds health scan limit")
            break
        if path.stem in present:
            continue
        try:
            job = queue.review_record(path)
            if job["state"] == "attention":
                jobs.append(job)
        except (CirraError, OSError, ValueError, TypeError, OverflowError):
            unreadable.append(path.name)
    return jobs, unreadable


def check(
    state_dir,
    *,
    max_heartbeat_age=DEFAULT_HEARTBEAT_AGE,
    attention_window=DEFAULT_ATTENTION_WINDOW,
    now=None,
    include_metrics=False,
    audit_policies=None,
):
    now = now or datetime.now(UTC)
    queue = Queue(state_dir)
    root = queue.directory.parent
    problems = []

    status_path = root / "worker-status.json"
    heartbeat = None
    status = {}
    if status_path.exists():
        try:
            status = _record(queue, status_path)
            heartbeat = _parse(status.get("heartbeat_at"))
            if status.get("state") == "waiting_for_quorum":
                problems.append(
                    {"kind": "coordination", "detail": "worker cannot confirm quorum ownership"}
                )
            if status.get("state") == "waiting_for_private_state":
                problems.append(
                    {
                        "kind": "coordination",
                        "detail": "no worker owns a verified current private state",
                    }
                )
        except (CirraError, OSError, ValueError):
            heartbeat = None
    if heartbeat is None:
        problems.append({"kind": "heartbeat", "detail": "no readable worker heartbeat"})
    elif heartbeat > now + timedelta(minutes=1):
        problems.append({"kind": "heartbeat", "detail": "worker heartbeat is in the future"})
    elif now - heartbeat > timedelta(seconds=max_heartbeat_age):
        age = int((now - heartbeat).total_seconds())
        problems.append({"kind": "heartbeat", "detail": f"worker heartbeat is {age} s old"})
    backup_problem = _backup_problem(queue, status, now)
    if backup_problem:
        problems.append(backup_problem)
    restore_problem = _restore_problem(queue, status, now)
    if restore_problem:
        problems.append(restore_problem)

    jobs, unreadable = queue.scan()
    if unreadable:
        problems.append({"kind": "queue", "detail": f"unreadable queue records: {unreadable}"})
    metrics = None
    try:
        metrics = queue_metrics(queue, now=now, scanned=(jobs, unreadable), history=False)
        if metrics["unreadable_cooldowns"]:
            problems.append(
                {"kind": "provider", "detail": "unreadable provider cooldown is holding work"}
            )
        if metrics["census_pending"]:
            problems.append(
                {
                    "kind": "capacity",
                    "detail": "worker is counting legacy queue records before new admission",
                }
            )
        elif metrics["remaining"] <= metrics["capacity"] * 0.1:
            problems.append(
                {
                    "kind": "capacity",
                    "detail": f"queue has {metrics['remaining']} admission slots left",
                }
            )
        if metrics["oldest_eligible_seconds"] > DELIVERY_TARGET_SECONDS:
            problems.append(
                {"kind": "backlog", "detail": "eligible work has been queued for over 30 minutes"}
            )
        refusal = _parse(metrics["last_refusal"])
        if refusal and now - refusal < timedelta(minutes=15):
            problems.append(
                {
                    "kind": "admission",
                    "detail": "webhook queue refused admission in the last 15 minutes",
                }
            )
    except (CirraError, OSError, TypeError, ValueError, KeyError):
        problems.append({"kind": "metrics", "detail": "unreadable queue operations records"})
    disk = shutil.disk_usage(root)
    if disk.free < min(disk.total // 20, 1024**3):
        problems.append(
            {"kind": "disk", "detail": "state storage has less than its free-space reserve"}
        )
    since = now - timedelta(seconds=attention_window) if attention_window is not None else None
    archived, archive_unreadable = _archived_review_failures(queue, jobs)
    if archive_unreadable:
        problems.append(
            {"kind": "queue", "detail": f"unreadable archived records: {archive_unreadable}"}
        )
    for job in [*jobs, *archived]:
        updated = _parse(job.get("updated_at"))
        if job.get("state") == "attention" and (since is None or (updated and updated >= since)):
            problems.append({"kind": "attention", "job": job.get("id"), "detail": job.get("error")})

    audit_queue = AuditQueue(state_dir)
    audit_jobs, audit_unreadable = audit_queue.scan()
    archived_audits, archived_unreadable = audit_queue.scan(audit_queue.directory / "archive")
    current_ids = {job["id"] for job in audit_jobs}
    audit_jobs.extend(job for job in archived_audits if job["id"] not in current_ids)
    audit_unreadable.extend("archive/" + name for name in archived_unreadable)
    if audit_unreadable:
        problems.append(
            {"kind": "audit-queue", "detail": f"unreadable audit records: {audit_unreadable}"}
        )
    for job in audit_jobs:
        updated = _parse(job.get("updated_at"))
        if job.get("state") in ("attention", "failed") and (
            since is None or (updated and updated >= since)
        ):
            detail = job.get("error") or (job.get("result") or {}).get("publication")
            problems.append({"kind": "audit", "job": job.get("id"), "detail": detail})

    reconcile_path = root / "reconciliation.json"
    if reconcile_path.exists():
        try:
            routes = queue.read(reconcile_path).get("routes", [])
        except (OSError, ValueError):
            routes = [{"route": "?", "error": "unreadable reconciliation record"}]
        for row in routes:
            if "error" in row or row.get("branch_errors"):
                problems.append(
                    {
                        "kind": "reconcile",
                        "route": row.get("route"),
                        "detail": row.get("error") or row.get("branch_errors"),
                    }
                )

    usage = queue.usage()
    if usage.get("maximum") and usage.get("attempts", 0) >= usage["maximum"]:
        problems.append(
            {"kind": "daily", "detail": f"daily admission exhausted ({usage['attempts']})"}
        )
    audit_metrics, warnings = _audit_metrics(root, audit_jobs, now, audit_policies)
    result = {"healthy": not problems, "problems": problems, "warnings": warnings}
    if include_metrics:
        result["queue_metrics"] = metrics
        result["audit_metrics"] = audit_metrics
        result["recovery_metrics"] = _recovery_metrics(queue, status, now)
    return result


def main(state_dir, max_heartbeat_age, attention_window, service_config=None):
    policies = None
    if service_config is not None:
        policies = {}
        for route in service_config.routes.values():
            if route.policy.audit is not None:
                key = repository_key(
                    {
                        name: getattr(route, name)
                        for name in ("provider", "instance", "repository_id")
                    }
                )
                age = route.policy.audit.max_age_days
                policies[key] = min(policies.get(key, age), age)
    result = check(
        state_dir,
        max_heartbeat_age=max_heartbeat_age,
        attention_window=attention_window,
        include_metrics=True,
        audit_policies=policies,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["healthy"] else 1
