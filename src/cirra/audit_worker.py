"""Worker passes for production-audit requests: claim, run, settle.

A runner is `run(job, attempt, stop)` and returns one of
`{"state": "awaiting_import"}` (an operator's session will import the report),
`{"state": "imported", "audit": summary}` (the runner imported a report), or
`{"state": "stopped", "reason": ...}` after `stop()` returned a reason at a
checkpoint. Cancellation and supersession are cooperative: `stop()` reports
them and the attempt's stored state already refuses a late import.
"""

from cirra import audits
from cirra.domain import CirraError

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
    if attempt["state"] == "complete" and on_complete:
        result["publication"] = on_complete(job, attempt)
    return queue.update(job["id"], state=FINAL[attempt["state"]], result=result)


def audit_once(queue, store, maximum, run, *, on_complete=None, now=None):
    """Settle imported session audits, then run at most one runnable request."""
    for job in queue.jobs():
        if job["state"] == "awaiting_import":
            settle(queue, store, job, on_complete)
    job = queue.next_job(maximum, now)
    if job is None:
        return None
    attempt, _ = audits.begin(store, job["identity"])
    job = queue.update(job["id"], attempt_id=attempt["attempt_id"])
    if attempt["state"] == "complete":
        return settle(queue, store, job, on_complete)  # Attached to an existing result.
    try:
        outcome = run(job, attempt, lambda: queue.stop_reason(job["id"]))
    except CirraError as exc:
        audits.mark(store, attempt["attempt_id"], "cancelled")
        return queue.update(job["id"], state="failed", error=str(exc))
    if outcome["state"] == "awaiting_import":
        return queue.update(job["id"], state="awaiting_import")
    if outcome["state"] == "stopped":
        reason = outcome["reason"]
        if reason in ("cancelled", "superseded"):
            audits.mark(store, attempt["attempt_id"], reason)
            return queue.update(job["id"], state=reason)
        raise CirraError(f"unsupported runner stop reason {reason!r}")
    if outcome["state"] == "imported":
        return settle(queue, store, queue.read(job["id"]), on_complete)
    raise CirraError("audit runner returned an unsupported outcome")
