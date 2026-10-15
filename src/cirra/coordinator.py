"""Coordinate a frozen offline snapshot, one reviewer attempt, and refreshed CI evidence."""

import json
import time

from cirra.domain import CirraError, digest
from cirra.policy import assess_checks
from cirra.report import make_report
from cirra.reviewer import PROTOCOL, FakeReviewer, validate_response


def failed_review(outcome, message):
    return {"outcome": outcome, "findings": [], "notes": [], "errors": [message]}


def analysis_identity(provider, config):
    packet = provider.packet()
    return {
        "snapshot_digest": digest(provider.snapshot.record()),
        "input_digest": digest(packet),
        "config_digest": config.digest,
        "prompt_digest": digest(PROTOCOL),
        "reviewer": {
            "kind": "fake",
            "endpoint": "fake://local",
            "model": config.model,
            "version": PROTOCOL,
            "fixture_response_digest": provider.response_digest,
        },
    }


def review(provider, config, store, *, rereview=False, reviewer=None):
    if config.reviewer_kind != "fake":
        raise CirraError("session reviewers use prepare-review followed by import-review")
    snapshot = provider.snapshot
    identity = analysis_identity(provider, config)
    reviewer = reviewer if reviewer is not None else FakeReviewer(provider.response)
    with store.lease(snapshot.change_key, config.claim_ttl_seconds, config.wait_seconds) as claim:
        attempt, reused = store.attempt(claim, digest(identity), identity, rereview)
        run_id = attempt["run_id"]
        duration = attempt["duration_seconds"]
        if attempt["state"] == "prepared":
            store.begin_call(claim, run_id)
            started = time.monotonic()
            try:
                raw = reviewer.review(provider.packet())
                if not isinstance(raw, str) or len(raw.encode()) > config.max_response_bytes:
                    envelope = {
                        "error": "malformed",
                        "message": "reviewer response byte/type limit exceeded",
                    }
                else:
                    envelope = {"raw": raw}
            except TimeoutError:
                envelope = {"error": "timeout", "message": "reviewer deadline exceeded"}
            except Exception:
                # A transport exception may hide a completed call. Never automatically spend again.
                envelope = {
                    "error": "uncertain",
                    "message": "reviewer call outcome is unknown; use --rereview",
                }
            duration = time.monotonic() - started
            store.record_response(claim, run_id, envelope, duration)
            attempt["state"] = "responded"
            attempt["response_json"] = json.dumps(envelope)
        if attempt["state"] == "calling":
            result = failed_review(
                "uncertain",
                "previous call lost its response; inspect the run and use --rereview explicitly",
            )
            store.mark_uncertain(claim, run_id, result)
        elif attempt["state"] == "responded":
            envelope = json.loads(attempt["response_json"])
            result = (
                failed_review(envelope["error"], envelope["message"])
                if "error" in envelope
                else validate_response(envelope["raw"], snapshot, config)
            )
            store.finish(claim, run_id, result)
        else:
            result = json.loads(attempt["result_json"])
        result = {
            **result,
            "attempt": attempt["number"],
            "duration_seconds": duration,
            "model": identity["reviewer"],
            "usage": None,
            "cost": None,
            "cost_note": "offline fixture; no billable model call",
        }
        ci = assess_checks(provider.checks, snapshot, config, provider.check_errors)
        report = make_report(
            snapshot, ci, result, identity, run_id, config, collection=provider.collection
        )
        store.save_report(claim, report)
    return report, reused
