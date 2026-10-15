"""Evidence-bound requests to a provider-owned release workflow; no release executor."""

import json
from urllib.parse import quote

from cirra.domain import CirraError, digest, number, string, utc_now
from cirra.policy import assess_checks
from cirra.publish import write_receipt


def plan(report, workflow_id, ref):
    number(workflow_id, "workflow ID", 1, 2**63 - 1, integral=True)
    string(ref, "release ref", 250)
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
            "Workflow must accept cirra_request_id/cirra_expected_sha, "
            "name the run cirra-release-<request_id>, and verify checkout before release."
        ),
    }
    request["request_id"] = digest(request)
    return request


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
        {key: value for key, value in request.items() if key != "request_id"}
    ) != request.get("request_id"):
        raise CirraError("release request digest does not match")
    saved = store.latest_report(request["run_id"])
    if (
        saved["analysis_digest"] != request["analysis_digest"]
        or saved["snapshot"] != request["snapshot"]
        or saved["inputs"]["config_digest"] != config.digest
        or saved.get("collection", {}).get("repository") != request["repository"]
    ):
        raise CirraError("release request no longer matches the saved review and policy")
    plan(saved, request["workflow_id"], request["ref"])
    snapshot = request["snapshot"]
    key = digest(
        [snapshot[name] for name in ("provider", "instance", "repository_id", "change_id")]
    )
    path = store.path.parent / "releases" / f"{request['request_id']}.json"
    with store.lease(key, config.claim_ttl_seconds, config.wait_seconds) as claim:
        fresh = collector()
        if (
            fresh.snapshot.record() != snapshot
            or assess_checks(fresh.checks, fresh.snapshot, config, fresh.check_errors)["outcome"]
            != "verified"
        ):
            raise CirraError("release evidence is stale or CI is no longer verified")
        prefix = "/repos/" + request["repository"]
        # Mutable refs can move after this read. The native workflow must checkout
        # the expected SHA explicitly, and enforce its normal release approvals.
        commit, _ = (reader_http or http).request(
            "GET", prefix + "/commits/" + quote(request["ref"], safe="")
        )
        if commit.get("sha") != request["expected_sha"]:
            raise CirraError("release ref does not point to the examined revision")
        with store.transaction() as connection:
            store._fence(connection, claim)
            row = connection.execute(
                "SELECT run_id FROM attempts WHERE change_key=? ORDER BY rowid DESC LIMIT 1", (key,)
            ).fetchone()
            if not row or row["run_id"] != request["run_id"]:
                raise CirraError("a newer review supersedes this release request")
            previous = json.loads(path.read_text()) if path.exists() else None
            if previous or reconcile:
                matches = []
                for page in range(1, 11):
                    runs, more = http.request(
                        "GET",
                        prefix + f"/actions/workflows/{request['workflow_id']}/runs",
                        {"event": "workflow_dispatch", "per_page": 100, "page": page},
                    )
                    matches.extend(
                        item
                        for item in runs.get("workflow_runs", [])
                        if item.get("display_title") == "cirra-release-" + request["request_id"]
                        and item.get("head_sha") == request["expected_sha"]
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
                        "observed_at": utc_now(),
                    }
                    write_receipt(path, receipt)
                    return receipt
                if len(matches) > 1:
                    raise CirraError(
                        "multiple native release runs match; inspect before further dispatch"
                    )
                if reconcile or not retry_uncertain:
                    return previous or {"state": "unresolved", "request_id": request["request_id"]}
            receipt = {
                "state": "uncertain",
                "request_id": request["request_id"],
                "intent_at": utc_now(),
                "duplicate_risk_accepted": retry_uncertain,
            }
            write_receipt(path, receipt)
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
            except CirraError as exc:
                receipt["reason"] = str(exc)
            else:
                receipt["state"] = "accepted"
                receipt["reason"] = (
                    "Provider accepted the request; reconcile to observe the native run."
                )
            write_receipt(path, receipt)
            return receipt
