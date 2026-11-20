from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from cirra.config import MergeGate, load_config
from cirra.domain import CirraError


def test_gate_policy_requires_real_review_and_a_pinned_observer(policy_data, tmp_path):
    data = deepcopy(policy_data)
    data["repositories"] = [
        {"provider": "github", "instance": "https://github.com", "repository_id": "42"}
    ]
    data["reviewer"] = {
        "kind": "claude",
        "endpoint": "cli://claude",
        "model": "sonnet",
        "merge_gate": {"branch": "main", "block_at": "high"},
    }
    data["ci"] = {
        "mode": "required",
        "expected": [
            {
                "kind": "check_run",
                "source_id": "42",
                "name": "cirra/isolated-tests",
                "definition_digest": "a" * 64,
            }
        ],
    }
    path = tmp_path / "policy.yaml"

    def read():
        path.write_text(yaml.safe_dump(data))
        return load_config(path)

    assert read().merge_gate.branch == "main"
    assert read().merge_gate.max_intermediate_comparisons == 32
    for valid in (0, 2, 100):
        data["reviewer"]["merge_gate"]["max_intermediate_comparisons"] = valid
        assert read().merge_gate.max_intermediate_comparisons == valid
    for invalid in (-1, 101, True, 1.5):
        data["reviewer"]["merge_gate"]["max_intermediate_comparisons"] = invalid
        with pytest.raises(CirraError, match="intermediate comparison limit"):
            read()
    del data["reviewer"]["merge_gate"]["max_intermediate_comparisons"]
    data["limits"]["max_findings"] = 0
    with pytest.raises(CirraError, match="gate"):
        read()
    data["limits"]["max_findings"] = 3
    data["ci"]["expected"][0].pop("definition_digest")
    with pytest.raises(CirraError, match="gate"):
        read()
    data["ci"]["expected"][0]["name"] = "cirra/code-review"
    with pytest.raises(CirraError, match="own output"):
        read()


@pytest.mark.parametrize(
    "defect",
    ["none", "high", "incomplete", "timeout", "unverified", "merge_exact", "notes", "fake"],
)
def test_required_verdict_fails_closed(defect):
    from cirra.review_gate import verdict

    report = {
        "status": "complete",
        "coverage": {"complete": True},
        "inputs": {"reviewer": {"kind": "claude"}},
        "review": {
            "outcome": "succeeded",
            "findings": [{"severity": "medium"}],
            "notes": [],
            "session": {"tool": "claude", "provenance": "cli_transport"},
        },
        "ci": {
            "outcome": "verified",
            "expected": [
                {
                    "name": "cirra/isolated-tests",
                    "definition_digest": "a" * 64,
                    "satisfied": True,
                    "evidence": {"relationship": "head_exact"},
                }
            ],
        },
    }
    if defect == "high":
        report["review"]["findings"][0]["severity"] = "high"
    elif defect == "incomplete":
        report["coverage"]["complete"] = False
    elif defect == "timeout":
        report["review"]["outcome"] = "timeout"
    elif defect == "unverified":
        report["ci"]["outcome"] = "incomplete"
    elif defect == "merge_exact":
        report["ci"]["expected"][0]["evidence"]["relationship"] = "merge_exact"
    elif defect == "notes":
        report["review"]["notes"] = ["A section could not be reviewed."]
    elif defect == "fake":
        report["inputs"]["reviewer"]["kind"] = "fake"
    assert verdict(report, "high")[0] == ("success" if defect == "none" else "failure")


def test_gate_reopens_owned_check_and_only_completes_a_current_review(
    inputs, fixture_data, tmp_path
):
    from cirra.coordinator import review
    from cirra.review_gate import RequiredReviewCheck
    from cirra.store import Store
    from cirra.transport import RemoteUncertain

    current = fixture_data["change"]
    fixture_data["additional_comparisons"] = []
    check = fixture_data["checks"][0]
    check["name"] = "cirra/isolated-tests"
    check["provenance"]["definition_digest"] = "a" * 64
    fixture_data["collection"] = {
        "source": "github_rest",
        "repository": "example/repo",
        "observed_at": check["observed_at"],
        "check_errors": [],
        "target_branch": "main",
    }
    provider, config = inputs(data=fixture_data)
    config = replace(
        config,
        reviewer_kind="claude",
        merge_gate=MergeGate("main", "high"),
        expected_checks=(
            {k: check[k] for k in ("kind", "source_id", "name")} | {"definition_digest": "a" * 64},
        ),
    )
    store = Store(tmp_path / "state")
    reviewer = SimpleNamespace(
        review=lambda _: '{"findings":[],"notes":[]}',
        metadata={"session": {"tool": "claude", "provenance": "cli_transport"}},
    )
    saved, _ = review(provider, config, store, reviewer=reviewer)
    route = SimpleNamespace(
        policy=config,
        repository_id=current["repository_id"],
        repository="example/repo",
        publisher={"app_id": 42},
    )

    class HTTP:
        item = None
        posts = 0
        lose_ack = True

        def request(self, method, path, params=None, *, body=None):
            if method == "GET":
                assert params["check_name"] == "cirra/code-review"
                return {"check_runs": [self.item] if self.item else []}, False
            if method == "POST":
                self.posts += 1
                self.item = {"id": 11, "app": {"id": 42}, **body}
            else:
                assert path.endswith("/11")
                self.item.update(body)
            if self.lose_ack:
                self.lose_ack = False
                raise RemoteUncertain("lost acknowledgement")
            return self.item.copy(), False

    http = HTTP()
    gate = RequiredReviewCheck(tmp_path, route, current, lambda: http, lambda: "f" * 40)
    with pytest.raises(RemoteUncertain):
        gate.start()
    gate.start()
    assert http.posts == 1 and http.item["status"] == "in_progress"
    assert http.item["head_sha"] == current["head_sha"]
    provider.collection["target_branch"] = "different"
    with pytest.raises(CirraError, match="target branch"):
        gate.finish(store, saved, lambda: provider)
    provider.collection["target_branch"] = "main"
    assert gate.finish(store, saved, lambda: provider)["conclusion"] == "success"
    completed = http.item.copy()
    gate.start()
    assert http.item == completed
    provider.checks[0]["raw_state"] = "failure"
    assert gate.finish(store, saved, lambda: provider)["conclusion"] == "failure"
    http.item["conclusion"] = "neutral"
    with pytest.raises(RemoteUncertain):
        gate.verify(http.item, "completed", "failure", "11")


def test_reconciliation_requires_published_review_for_this_target_and_policy(tmp_path):
    from cirra.job_queue import Queue
    from cirra.reconcile import sync

    route = SimpleNamespace(
        name="github",
        digest="v1",
        provider="github",
        instance="https://github.com",
        repository="example/repo",
        repository_id="42",
        reader_env=None,
        policy=SimpleNamespace(merge_gate=MergeGate("main", "high")),
    )
    queue = Queue(tmp_path)
    service = SimpleNamespace(routes={"github": route})
    revision = {"target_branch": "main", "head": "a" * 40, "target": "b" * 40, "merge": "d" * 40}

    def poll():
        return sync(service, queue, lambda _: None, discover_changes=lambda *_: [(7, revision)])[0]

    queued = poll()["jobs"][0]
    job = queue.read(queue.directory / (queued["job_id"] + ".json"))
    job.update(state="done", result={"ci": {"head_sha": revision["head"], "change": 7}})
    queue.save(job)
    assert not queue.tested_head(route, 7, revision["head"], review_target=revision["target"])
    job["result"]["review_gate"] = {
        "state": "published",
        "head_sha": revision["head"],
        "target_sha": revision["target"],
        "merge_sha": revision["merge"],
    }
    queue.save(job)
    assert poll()["already_tested"] == [7]
    revision["merge"] = "e" * 40
    assert not poll()["jobs"][0]["duplicate"]
    revision["merge"] = "d" * 40
    revision["target"] = "c" * 40
    assert not poll()["jobs"][0]["duplicate"]
    revision["target"] = "b" * 40
    route.digest = "v2"
    assert not poll()["jobs"][0]["duplicate"]


@pytest.mark.parametrize(
    "case",
    [
        "current",
        "retargeted",
        "new_base",
        "wrong_parents",
        "wrong_ref",
        "wrong_type",
        "draft",
        "conflict",
    ],
)
def test_native_merge_binding_requires_the_open_pr_and_ordered_parents(case):
    from cirra.review_gate import merge_revision

    base, head, merge = (c * 40 for c in "abc")
    change = {"change_id": "7", "repository_id": "42", "head_sha": head, "target_sha": base}
    pr = {
        "number": 7,
        "head": {"sha": head},
        "base": {"sha": base, "repo": {"id": 42}, "ref": "main"},
        "state": "open",
        "draft": case == "draft",
        "changed_files": 1,
        "mergeable": case != "conflict",
    }
    if case == "retargeted":
        pr["base"]["ref"] = "other"
    resources = {
        "pulls/7": pr,
        "git/ref/pull/7/merge": {
            "ref": "refs/pull/8/merge" if case == "wrong_ref" else "refs/pull/7/merge",
            "object": {"sha": merge, "type": "tag" if case == "wrong_type" else "commit"},
        },
        "branches/main": {"name": "main", "commit": {"sha": head if case == "new_base" else base}},
        "git/commits/" + merge: {
            "sha": merge,
            "parents": [
                {"sha": s} for s in ((head, base) if case == "wrong_parents" else (base, head))
            ],
        },
    }
    client = SimpleNamespace(get=lambda path: (resources[path], False))
    if case == "current":
        assert merge_revision(client, change, "main") == merge
    else:
        with pytest.raises(CirraError):
            merge_revision(client, change, "main")
