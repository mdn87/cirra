"""Operator health check over the service state directory, for a timer or monitor."""

import json
from datetime import UTC, datetime, timedelta

from cirra.job_queue import Queue

# A bounded test run plus review can hold the loop for several minutes, during
# which the heartbeat legitimately does not move.
DEFAULT_HEARTBEAT_AGE = 1200
DEFAULT_ATTENTION_WINDOW = 86400


def _parse(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def check(
    state_dir,
    *,
    max_heartbeat_age=DEFAULT_HEARTBEAT_AGE,
    attention_window=DEFAULT_ATTENTION_WINDOW,
    now=None,
):
    now = now or datetime.now(UTC)
    queue = Queue(state_dir)
    root = queue.directory.parent
    problems = []

    status_path = root / "worker-status.json"
    heartbeat = None
    if status_path.exists():
        try:
            heartbeat = _parse(queue.read(status_path).get("heartbeat_at"))
        except (OSError, ValueError):
            heartbeat = None
    if heartbeat is None:
        problems.append({"kind": "heartbeat", "detail": "no readable worker heartbeat"})
    elif now - heartbeat > timedelta(seconds=max_heartbeat_age):
        age = int((now - heartbeat).total_seconds())
        problems.append({"kind": "heartbeat", "detail": f"worker heartbeat is {age} s old"})

    jobs, unreadable = queue.scan()
    if unreadable:
        problems.append({"kind": "queue", "detail": f"unreadable queue records: {unreadable}"})
    since = now - timedelta(seconds=attention_window)
    for job in jobs:
        updated = _parse(job.get("updated_at"))
        if job.get("state") == "attention" and updated is not None and updated >= since:
            problems.append({"kind": "attention", "job": job.get("id"), "detail": job.get("error")})

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
    return {"healthy": not problems, "problems": problems}


def main(state_dir, max_heartbeat_age, attention_window):
    result = check(
        state_dir, max_heartbeat_age=max_heartbeat_age, attention_window=attention_window
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["healthy"] else 1
