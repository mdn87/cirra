"""Evidence-bound requests to trusted, provider-owned release workflows."""

import json
from urllib.parse import quote

from cirra.domain import CirraError, digest, number, sha, string, utc_now
from cirra.github import record
from cirra.policy import assess_checks
from cirra.publish import checkpoint
from cirra.transport import RemoteUncertain


def request_identity(request):
    return digest(
        {
            "repository_id": request["snapshot"]["repository_id"],
            "instance": request["snapshot"]["instance"],
            **{
                key: request[key]
                for key in ("workflow_id", "ref", "expected_sha", "analysis_digest")
            },
        }
    )


def plan(report, workflow_id, ref):
    number(workflow_id, "workflow ID", 1, 2**63 - 1, integral=True)
    string(ref, "release branch", 250)
    if ref.startswith("refs/"):
        raise CirraError("release ref must be a short protected target branch name")
    if (
        report["snapshot"]["provider"] != "github"
        or not report.get("collection")
        or report["status"] != "complete"
        or report["ci"]["outcome"] != "verified"
        or report["review"]["outcome"] != "succeeded"
        or report["review"]["findings"]
    ):
        raise CirraError(
            "release request needs a complete captured GitHub review, verified CI and no findings"
        )
    if not report["ci"]["expected"] or not all(
        item.get("definition_digest") and item.get("definition_verified")
        for item in report["ci"]["expected"]
    ):
        raise CirraError(
            "release requires witness-verified CI definition digests in expected-check policy"
        )
    request = {
        "schema_version": 1,
        "run_id": report["run_id"],
        "analysis_digest": report["analysis_digest"],
        "report_digest": report["report_digest"],
        "snapshot": report["snapshot"],
        "repository": report["collection"]["repository"],
        "workflow_id": workflow_id,
        "ref": ref,
        "expected_sha": report["snapshot"]["head_sha"],
        "created_at": utc_now(),
        "contract": (
            "Merged candidate reachable from a protected target branch; dispatch its trusted "
            "workflow and checkout cirra_expected_sha. "
            "Name the run cirra-release-<cirra_request_id>."
        ),
    }
    request["request_id"] = request_identity(request)
    request["request_digest"] = digest(request)
    return request


def trusted_workflow_revision(reader, prefix, request):
    """PR workflow YAML never chooses the release execution definition."""
    snapshot = request["snapshot"]
    pr, _ = reader.request("GET", prefix + "/pulls/" + snapshot["change_id"])
    record(pr)
    base, head = record(pr.get("base")), record(pr.get("head"))
    if (
        pr.get("merged") is not True
        or head.get("sha") != request["expected_sha"]
        or base.get("ref") != request["ref"]
        or str(record(base.get("repo")).get("id")) != snapshot["repository_id"]
    ):
        raise CirraError(
            "release candidate must be merged into the selected protected target branch"
        )
    branch, _ = reader.request("GET", prefix + "/branches/" + quote(request["ref"], safe=""))
    record(branch)
    if branch.get("protected") is not True:
        raise CirraError("release workflow ref is not a protected branch")
    trusted_sha = sha(record(branch.get("commit")).get("sha"))
    comparison, _ = reader.request(
        "GET", prefix + f"/compare/{request['expected_sha']}...{trusted_sha}"
    )
    record(comparison)
    if (
        comparison.get("status") not in ("ahead", "identical")
        or record(comparison.get("merge_base_commit")).get("sha") != request["expected_sha"]
    ):
        raise CirraError("reviewed candidate is not reachable from the protected target branch")
    return trusted_sha


def dispatch(
    request,
    store,
    config,
    collector,
    http,
    *,
    reader_http=None,
    reconcile=False,
    retry_uncertain=False,
):
    if not isinstance(request, dict) or digest(
        {key: value for key, value in request.items() if key != "request_digest"}
    ) != request.get("request_digest"):
        raise CirraError("release request digest does not match")
    saved = store.latest_report(request["run_id"])
    if (
        saved["analysis_digest"] != request["analysis_digest"]
        or saved["snapshot"] != request["snapshot"]
        or saved["inputs"]["config_digest"] != config.digest
        or saved.get("collection", {}).get("repository") != request["repository"]
        or request["expected_sha"] != saved["snapshot"]["head_sha"]
        or request["request_id"] != request_identity(request)
    ):
        raise CirraError("release request no longer matches the saved review and policy")
    plan(saved, request["workflow_id"], request["ref"])
    snapshot = request["snapshot"]
    key = digest(
        [snapshot[name] for name in ("provider", "instance", "repository_id", "change_id")]
    )
    path = store.path.parent / "releases" / f"{request['request_id']}.json"
    with store.lease(key, config.claim_ttl_seconds, config.wait_seconds) as claim:
        store.latest_attempt_order(claim, request["run_id"])
        fresh = collector()
        if (
            fresh.snapshot.record() != snapshot
            or assess_checks(fresh.checks, fresh.snapshot, config, fresh.check_errors)["outcome"]
            != "verified"
        ):
            raise CirraError("release evidence is stale or CI is no longer verified")
        prefix = "/repos/" + request["repository"]
        trusted_sha = trusted_workflow_revision(reader_http or http, prefix, request)
        previous = json.loads(path.read_text()) if path.exists() else None
        if previous or reconcile:
            matches = []
            for page in range(1, 11):
                runs, more = http.request(
                    "GET",
                    prefix + f"/actions/workflows/{request['workflow_id']}/runs",
                    {"event": "workflow_dispatch", "per_page": 100, "page": page},
                )
                values = record(runs).get("workflow_runs")
                if not isinstance(values, list):
                    raise CirraError("malformed native workflow run list")
                matches.extend(
                    item
                    for item in values
                    if record(item).get("display_title") == "cirra-release-" + request["request_id"]
                    and item.get("head_sha") == (previous or {}).get("workflow_sha", trusted_sha)
                )
                if not more:
                    break
            else:
                raise CirraError("release reconciliation page limit reached")
            if len(matches) == 1:
                receipt = {
                    "state": "observed",
                    "request_id": request["request_id"],
                    "run_id": str(matches[0]["id"]),
                    "workflow_sha": matches[0]["head_sha"],
                    "trust": "merged_into_protected_target",
                    "observed_at": utc_now(),
                }
                checkpoint(store, claim, path, receipt)
                return receipt
            if len(matches) > 1:
                raise CirraError(
                    "multiple native release runs match; inspect before further dispatch"
                )
            if previous and previous["state"] in ("accepted", "observed"):
                return {
                    **previous,
                    "reason": "Already acknowledged; reconcile only. Check run visibility/name.",
                }
            if reconcile or (previous and previous["state"] == "uncertain" and not retry_uncertain):
                return previous or {"state": "unresolved", "request_id": request["request_id"]}
        receipt = {
            "state": "uncertain",
            "request_id": request["request_id"],
            "workflow_sha": trusted_sha,
            "trust": "merged_into_protected_target",
            "intent_at": utc_now(),
            "duplicate_risk_accepted": bool(previous and retry_uncertain),
        }
        checkpoint(store, claim, path, receipt)
        try:
            http.request(
                "POST",
                prefix + f"/actions/workflows/{request['workflow_id']}/dispatches",
                body={
                    "ref": request["ref"],
                    "inputs": {
                        "cirra_request_id": request["request_id"],
                        "cirra_expected_sha": request["expected_sha"],
                    },
                },
            )
        except RemoteUncertain as exc:
            receipt["reason"] = str(exc)
        except CirraError as exc:
            receipt.update(state="failed", reason=str(exc))
        else:
            receipt.update(
                state="accepted",
                reason="Provider accepted the request; reconcile to observe the native run.",
            )
        checkpoint(store, claim, path, receipt)
        return receipt
