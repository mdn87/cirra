"""An operator-enabled GitHub check for a live worker review of one target branch."""

import json

from cirra.audit import SEVERITIES
from cirra.domain import CirraError, digest, sha
from cirra.github import native_id, record, revisions, target_revision
from cirra.policy import assess_checks
from cirra.publish import write_receipt
from cirra.report import make_report, plain
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
    merge = sha(pr.get("merge_commit_sha"))
    commit, _ = client.get(f"git/commits/{merge}")
    if (
        record(commit).get("sha") != merge
        or not isinstance(commit.get("parents"), list)
        or [record(p).get("sha") for p in commit["parents"]] != [change["target_sha"], head]
    ):
        raise CirraError("GitHub test merge parents differ from the reviewed target and head")
    return merge


def verdict(report, threshold):
    review, ci = report["review"], report["ci"]
    session = review.get("session") or {}
    if (
        review["outcome"] != "succeeded"
        or report["inputs"]["reviewer"]["kind"] != "claude"
        or session.get("tool") != "claude"
        or session.get("provenance") != "cli_transport"
    ):
        return "failure", "The automatic code reviewer did not complete successfully."
    if not report["coverage"]["complete"] or review["notes"]:
        return "failure", "The reviewer could not cover the entire change."
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
        return "failure", "The worker could not verify passing CI on this exact commit."
    blocking = [
        f
        for f in review["findings"]
        if SEVERITIES.index(f["severity"]) >= SEVERITIES.index(threshold)
    ]
    if blocking:
        return (
            "failure",
            f"The review found {len(blocking)} issue(s) at {threshold} severity or above.",
        )
    return (
        "success",
        f"The reviewer covered the change and found no issues at {threshold} severity or above.",
    )


class RequiredReviewCheck:
    def __init__(self, directory, route, change, transport, current_merge):
        self.route, self.change, self.transport = route, change, transport
        self.current_merge = current_merge
        self.merge = current_merge()
        self.slot = digest([NAME, route.repository_id, route.policy.merge_gate.branch, self.merge])
        self.path = directory / "review-gates" / (self.slot + ".json")
        self.prefix = "/repos/" + route.repository

    def verify(self, item, status, conclusion=None, identifier=None):
        if (
            record(item).get("name") != NAME
            or item.get("external_id") != self.slot
            or item.get("head_sha") != self.merge
            or str(record(item.get("app")).get("id")) != str(self.route.publisher["app_id"])
            or item.get("status") != status
            or item.get("conclusion") != conclusion
            or (identifier is not None and str(item.get("id")) != identifier)
        ):
            raise RemoteUncertain(
                "required review acknowledgement does not match this worker check"
            )
        return native_id(item.get("id"))

    def start(self):
        http = self.transport()

        def get(path, params):
            data, more = http.request("GET", path, params)
            return record(data).get("check_runs"), more

        matches = [
            item
            for item in pages(
                get,
                f"{self.prefix}/commits/{self.merge}/check-runs",
                params={"filter": "all", "check_name": NAME},
            )
            if item.get("name") == NAME
            and item.get("external_id") == self.slot
            and item.get("head_sha") == self.merge
            and str(record(item.get("app")).get("id")) == str(self.route.publisher["app_id"])
        ]
        prior = json.loads(self.path.read_text()) if self.path.exists() else {}
        if len(matches) > 1 or (prior and prior["state"] != "refused" and not matches):
            raise CirraError("required review check has no unique visible owner after a write")
        identifier = native_id(matches[0]["id"]) if matches else None
        if prior.get("native_id") and prior["native_id"] != identifier:
            raise CirraError("required review check changed its native identity")
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
            body["head_sha"] = self.merge
        write_receipt(self.path, {"state": "starting", "native_id": identifier}, history=False)
        try:
            item, _ = http.request("PATCH" if identifier else "POST", path, body=body)
        except RemoteUncertain:
            raise
        except CirraError:
            if not identifier:
                write_receipt(self.path, {"state": "refused", "native_id": None})
            raise
        self.identifier = self.verify(item, "in_progress", identifier=identifier)
        write_receipt(self.path, {"state": "in_progress", "native_id": self.identifier})

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
            conclusion, reason = verdict(report, config.merge_gate.block_at)
            summary = [
                reason,
                "",
                f"PR #{self.change['change_id']} targeting {plain(branch)}.",
                f"Head `{self.change['head_sha']}` · target `{self.change['target_sha']}`",
                f"Review `{saved['run_id']}` · report `{report['report_digest']}`",
            ]
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
            item, _ = http.request(
                "PATCH", f"{self.prefix}/check-runs/{self.identifier}", body=body
            )
            self.verify(item, "completed", conclusion, self.identifier)
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
            write_receipt(self.path, result)
            return result
