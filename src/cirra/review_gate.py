"""An operator-enabled GitHub check for a live worker review of one target branch."""

import json
import uuid

from cirra.audit import SEVERITIES
from cirra.domain import CirraError, digest
from cirra.github import native_id, pull_merge_ref, record, revisions, target_revision
from cirra.policy import assess_checks
from cirra.publish import write_receipt
from cirra.report import (
    check_result,
    ci_next_step,
    coverage_next_step,
    excerpt,
    make_report,
    plain,
    reviewer_next_step,
    with_next_step,
)
from cirra.transport import RemoteUncertain, pages

NAME = "cirra/code-review"


def merge_revision(client, change, branch):
    """Read GitHub's test merge for this open PR and verify its ordered parents."""
    number = int(change["change_id"])
    pr, _ = client.get(f"pulls/{number}")
    head, _, _ = revisions(pr, change["repository_id"], number)
    if (
        pr.get("state") != "open"
        or pr.get("draft") is not False
        or pr["base"].get("ref") != branch
        or head != change["head_sha"]
        or target_revision(client, branch) != change["target_sha"]
        or pr.get("mergeable") is not True
    ):
        raise CirraError("GitHub has no current test merge for this open pull request")
    merge = pull_merge_ref(client, number)
    commit, _ = client.get(f"git/commits/{merge}")
    if (
        record(commit).get("sha") != merge
        or not isinstance(commit.get("parents"), list)
        or [record(p).get("sha") for p in commit["parents"]] != [change["target_sha"], head]
    ):
        raise CirraError("GitHub test merge parents differ from the reviewed target and head")
    return merge


def verdict(report, threshold):
    """Return the conclusion and Markdown summary from the first applicable gate rule."""
    review, ci = report["review"], report["ci"]
    session = review.get("session") or {}
    if (
        review["outcome"] != "succeeded"
        or report["inputs"]["reviewer"]["kind"] != "claude"
        or session.get("tool") != "claude"
        or session.get("provenance") != "cli_transport"
    ):
        return "failure", with_next_step(
            "Cirra could not verify a completed automatic code review.", reviewer_next_step(review)
        )
    if not report["coverage"]["complete"] or review["notes"]:
        return "failure", with_next_step(
            "The reviewer could not cover the entire change.", coverage_next_step(report)
        )
    if (
        report["status"] != "complete"
        or ci["outcome"] != "verified"
        or not any(
            item["name"] == "cirra/isolated-tests"
            and item.get("definition_digest")
            and item["satisfied"]
            and item["evidence"]["relationship"] == "head_exact"
            for item in ci["expected"]
        )
    ):
        return "failure", with_next_step(
            "The worker could not verify passing CI on this exact commit.",
            ci_next_step(ci)
            or "Verify that the configured cirra/isolated-tests check passed on the exact PR head.",
        )
    blocking = [
        f
        for f in review["findings"]
        if SEVERITIES.index(f["severity"]) >= SEVERITIES.index(threshold)
    ]
    if blocking:
        finding = blocking[0]
        return "failure", with_next_step(
            f"The review found {len(blocking)} issue(s) at {threshold} severity or above.",
            f"Address the {finding['severity']} finding “{excerpt(finding['title'])}” "
            "and submit the new revision for review.",
        )
    return "success", with_next_step(
        f"The reviewer covered the change and found no issues at {threshold} severity or above.",
        "Consult GitHub for approval and merge requirements now that Cirra’s checks passed "
        "on this revision.",
    )


class RequiredReviewCheck:
    def __init__(self, directory, route, change, transport, current_merge):
        self.route, self.change, self.transport = route, change, transport
        self.current_merge = current_merge
        self.merge = current_merge()
        self.head = change["head_sha"]
        self.slot = digest([NAME, route.repository_id, route.policy.digest, self.head])
        self.path = directory / "review-gates" / (self.slot + ".json")
        self.prefix = "/repos/" + route.repository

    def verify(self, item, status, conclusion=None, identifier=None):
        if (
            record(item).get("name") != NAME
            or item.get("external_id") != self.slot
            or item.get("head_sha") != self.head
            or str(record(item.get("app")).get("id")) != str(self.route.publisher["app_id"])
            or item.get("status") != status
            or item.get("conclusion") != conclusion
            or (identifier is not None and str(item.get("id")) != identifier)
        ):
            raise RemoteUncertain(
                "required review acknowledgement does not match this worker check"
            )
        return native_id(item.get("id"))

    def confirm(self, item, intent):
        settled = intent["settled"]
        identifier = self.verify(
            item, intent["expected_status"], settled.get("conclusion"), settled.get("native_id")
        )
        output = item.get("output")
        if (
            not isinstance(output, dict)
            or digest({field: output.get(field) for field in ("title", "summary", "text")})
            != intent["output_digest"]
        ):
            raise RemoteUncertain("required review acknowledgement belongs to a different write")
        result = {**settled, "native_id": identifier}
        write_receipt(self.path, result)
        return result

    def send(self, http, method, path, body, settled):
        prior = json.loads(self.path.read_text()) if self.path.exists() else {}
        if prior.get("state") == "awaiting_ack":
            raise RemoteUncertain("previous required review write still needs provider readback")
        # Identical verdicts can recur. Only this marker identifies this particular request.
        operation = uuid.uuid4().hex
        body["output"]["text"] = f"<!-- cirra-operation:{operation} -->"
        intent = {
            "state": "awaiting_ack",
            # Until readback, identity includes the write as well as the object. Legacy
            # workers only accept a matching scalar GitHub ID, so they park this slot
            # on rollback even if GitHub is already showing a previous generation.
            "native_id": {"last_known_id": settled.get("native_id"), "operation_id": operation},
            "expected_status": body["status"],
            "output_digest": digest(body["output"]),
            "settled": {**settled, "operation_id": operation},
        }
        write_receipt(self.path, intent)
        try:
            item, _ = http.request(method, path, body=body)
        except RemoteUncertain:
            raise
        except CirraError:
            write_receipt(self.path, prior or {"state": "refused", "native_id": None})
            raise
        return self.confirm(item, intent)

    def start(self):
        http = self.transport()

        def get(path, params):
            data, more = http.request("GET", path, params)
            return record(data).get("check_runs"), more

        matches = [
            item
            for item in pages(
                get,
                f"{self.prefix}/commits/{self.head}/check-runs",
                params={"filter": "all", "check_name": NAME},
            )
            if item.get("name") == NAME
            and item.get("external_id") == self.slot
            and item.get("head_sha") == self.head
            and str(record(item.get("app")).get("id")) == str(self.route.publisher["app_id"])
        ]
        prior = json.loads(self.path.read_text()) if self.path.exists() else {}
        if len(matches) > 1 or (prior and prior["state"] != "refused" and not matches):
            raise CirraError("required review check has no unique visible owner after a write")
        identifier = native_id(matches[0]["id"]) if matches else None
        if prior.get("state") == "awaiting_ack":
            self.identifier = self.confirm(matches[0], prior)["native_id"]
            return
        if prior.get("native_id") and prior["native_id"] != identifier:
            raise CirraError("required review check changed its native identity")
        if matches and matches[0].get("status") == "completed":
            # GitHub keeps completed check runs terminal. On redelivery, refresh
            # the verdict in finish() without replacing the last report with a pending summary.
            conclusion = matches[0].get("conclusion")
            if conclusion not in ("success", "failure"):
                raise CirraError("existing required review has an unexpected conclusion")
            self.identifier = self.verify(matches[0], "completed", conclusion, identifier)
            return
        body = {
            "name": NAME,
            "external_id": self.slot,
            "status": "in_progress",
            "output": {
                "title": "Cirra code review",
                "summary": "The worker is building, testing and reviewing this revision.",
            },
        }
        path = self.prefix + "/check-runs"
        if identifier:
            path += "/" + identifier
        else:
            body["head_sha"] = self.head
        result = self.send(
            http,
            "PATCH" if identifier else "POST",
            path,
            body,
            {"state": "in_progress", "native_id": identifier},
        )
        self.identifier = result["native_id"]

    def finish(self, store, saved, refresh):
        config = self.route.policy
        with store.lease(
            digest(
                [self.change[k] for k in ("provider", "instance", "repository_id", "change_id")]
            ),
            config.claim_ttl_seconds,
            config.wait_seconds,
        ) as claim:
            provider = refresh()
            branch = config.merge_gate.branch
            if (
                provider.snapshot.record() != saved["snapshot"]
                or provider.additional_comparisons is None
                or provider.collection.get("target_branch") != branch
                or saved["inputs"]["config_digest"] != config.digest
                or any(self.change[k] != saved["snapshot"][k] for k in self.change)
            ):
                raise CirraError(
                    "required review snapshot, target branch or operator policy changed"
                )
            # A branch can return to a previously reviewed head. The refreshed
            # PR and test-merge identity fence other revisions independently.
            store.latest_attempt_order(
                claim, saved["run_id"], analysis_digest=saved["analysis_digest"]
            )
            ci = assess_checks(provider.checks, provider.snapshot, config, provider.check_errors)
            report = make_report(
                provider.snapshot,
                ci,
                saved["review"],
                saved["inputs"],
                saved["run_id"],
                config,
                collection=provider.collection,
            )
            store.save_report(claim, report)
            conclusion, explanation = verdict(report, config.merge_gate.block_at)
            summary = [
                explanation,
                "",
                f"CI evidence: {plain(ci['outcome'])}.",
                f"PR #{self.change['change_id']} targeting {plain(branch)}.",
                f"Head `{self.change['head_sha']}` · target `{self.change['target_sha']}`",
                f"Verified test merge `{self.merge}`.",
                f"Review `{saved['run_id']}` · report `{report['report_digest']}`",
            ]
            for label, details in (
                ("Coverage gap", report["coverage"]["gaps"]),
                ("Review note", report["review"]["notes"]),
                ("Review error", report["review"]["errors"]),
                ("CI collection error", ci["collection_errors"]),
            ):
                if details:
                    summary.append(f"{label} — {excerpt(details[0])}")
            for check in [item for item in ci["expected"] if not item["satisfied"]][:10]:
                summary.append(
                    f"- {excerpt(check['name'])} — {check_result(check)}. {plain(check['reason'])}."
                )
            for finding in report["review"]["findings"][:10]:
                summary.append(f"- {plain(finding['severity'])} — {plain(finding['title'][:200])}")
            body = {
                "status": "completed",
                "conclusion": conclusion,
                "external_id": self.slot,
                "output": {"title": "Cirra code review", "summary": "\n".join(summary)},
            }
            http = self.transport()
            if self.current_merge() != self.merge:
                raise CirraError("GitHub replaced the test merge during code review")
            result = {
                "state": "published",
                "conclusion": conclusion,
                "native_id": self.identifier,
                "head_sha": self.change["head_sha"],
                "target_sha": self.change["target_sha"],
                "merge_sha": self.merge,
                "run_id": saved["run_id"],
                "report_digest": report["report_digest"],
            }
            return self.send(
                http, "PATCH", f"{self.prefix}/check-runs/{self.identifier}", body, result
            )
