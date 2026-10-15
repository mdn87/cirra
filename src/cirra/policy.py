"""Assess expected CI identities without confusing association with tested revisions."""

from cirra.domain import canonical

STATES = {"success", "failure", "pending", "skipped", "cancelled", "inaccessible", "unknown"}


def relationship(check, snapshot):
    proof = check["provenance"]
    if proof["trusted"] and proof["tested_sha"] == snapshot.head_sha:
        return "head_exact"
    if (
        proof["trusted"]
        and proof["tested_sha"] is not None
        and proof["tested_sha"] != snapshot.head_sha
        and len(proof["parents"]) == 2
        and set(proof["parents"]) == {snapshot.target_sha, snapshot.head_sha}
        and snapshot.target_sha != snapshot.head_sha
    ):
        return "merge_exact"
    if check["associated_sha"] == snapshot.head_sha:
        return "associated_only"
    return "unrelated"


def assess_checks(checks, snapshot, config, collection_errors=()):
    evidence = []
    for check in checks:
        item = dict(check)
        item["state"] = check.get(
            "normalized_state", check["raw_state"] if check["raw_state"] in STATES else "unknown"
        )
        item["relationship"] = relationship(check, snapshot)
        item["excluded"] = check["name"] == "cirra/review"
        evidence.append(item)
    assessments = []
    for expected in config.expected_checks:
        candidates = [
            item
            for item in evidence
            if not item["excluded"]
            and all(item[key] == expected[key] for key in ("kind", "source_id", "name"))
        ]
        selected = None
        reason = "expected source has no evidence"
        state = "missing"
        if candidates:
            if len(candidates) == 1:
                latest = candidates
            elif any(item["started_at"] is None for item in candidates):
                latest = candidates  # Missing timestamps cannot safely order competing runs.
            else:
                newest = max(item["started_at"] for item in candidates)
                latest = [item for item in candidates if item["started_at"] == newest]
                if all(item["attempt"] is not None for item in latest):
                    attempt = max(item["attempt"] for item in latest)
                    latest = [item for item in latest if item["attempt"] == attempt]
            distinct = {canonical(item) for item in latest}
            if len(distinct) != 1:
                state, reason = "unknown", "ambiguous latest check evidence"
            else:
                selected = latest[0]
                state = selected["state"]
                reason = (
                    "trusted checkout matches the captured revision"
                    if selected["relationship"] in ("head_exact", "merge_exact")
                    else "native result is not verified against the captured revision"
                )
        verified = selected is not None and selected["relationship"] in (
            "head_exact",
            "merge_exact",
        )
        assessments.append(
            {
                **expected,
                "state": state,
                "verified_revision": verified,
                "satisfied": verified and state == "success",
                "reason": reason,
                "evidence": selected,
            }
        )
    if collection_errors:
        outcome = "incomplete"
    elif config.ci_mode == "review_only":
        outcome = "review_only"
    elif any(item["verified_revision"] and item["state"] == "failure" for item in assessments):
        outcome = "failed"
    elif all(item["satisfied"] for item in assessments):
        outcome = "verified"
    else:
        outcome = "incomplete"
    return {
        "outcome": outcome,
        "expected": assessments,
        "evidence": evidence,
        "collection_errors": list(collection_errors),
    }
