import copy
import socket

import pytest

from cirra.coordinator import review
from cirra.domain import CirraError
from cirra.policy import assess_checks
from cirra.report import render_markdown
from cirra.store import Store


def run_report(inputs, tmp_path, data=None, policy=None, **kwargs):
    provider, config = inputs(data, policy)
    return review(provider, config, Store(tmp_path / "state"), **kwargs)


def test_no_network_and_ci_refresh_does_not_repeat_review(
    inputs, fixture_data, tmp_path, monkeypatch
):
    def deny(*args, **kwargs):
        raise AssertionError("offline review attempted network access")

    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    first, _ = run_report(inputs, tmp_path)
    fixture_data["checks"][0]["raw_state"] = "failure"
    latest, reused = run_report(inputs, tmp_path, fixture_data)
    assert reused and first["run_id"] == latest["run_id"]
    assert latest["ci"]["outcome"] == "failed"
    assert latest["status"] == "complete"  # Complete evidence does not mean permission to merge.


@pytest.mark.parametrize("field", ["head_sha", "diff_base_sha", "target_sha"])
def test_revision_context_changes_analysis(field, inputs, fixture_data, tmp_path):
    first, _ = run_report(inputs, tmp_path)
    fixture_data["change"][field] = "3" * 40
    second, reused = run_report(inputs, tmp_path, fixture_data)
    assert not reused and first["analysis_digest"] != second["analysis_digest"]


def test_policy_and_patch_changes_invalidate_analysis(inputs, fixture_data, policy_data, tmp_path):
    first, _ = run_report(inputs, tmp_path)
    policy_data["reviewer"]["model"] = "another-fake-version"
    second, _ = run_report(inputs, tmp_path, policy=policy_data)
    assert second["analysis_digest"] != first["analysis_digest"]
    fixture_data["files"][0]["patch"] = fixture_data["files"][0]["patch"].replace("a - b", "b - a")
    third, _ = run_report(inputs, tmp_path, fixture_data, policy_data)
    assert third["analysis_digest"] != second["analysis_digest"]


@pytest.mark.parametrize(
    "state", ["pending", "skipped", "cancelled", "inaccessible", "unknown", "new-state"]
)
def test_non_success_states_remain_visible(state, inputs, fixture_data, tmp_path):
    fixture_data["checks"][0]["raw_state"] = state
    report, _ = run_report(inputs, tmp_path, fixture_data)
    assert report["status"] == "incomplete"
    check = report["ci"]["expected"][0]
    assert check["evidence"]["raw_state"] == state
    assert check["state"] == ("unknown" if state == "new-state" else state)


def test_missing_and_spoofed_sources_cannot_satisfy_policy(inputs, fixture_data, tmp_path):
    fixture_data["checks"][0]["source_id"] = "untrusted-app"
    report, _ = run_report(inputs, tmp_path, fixture_data)
    assert report["ci"]["expected"][0]["state"] == "missing"
    assert report["status"] == "incomplete"


def test_association_and_old_merge_do_not_verify_new_target(inputs, fixture_data):
    check = fixture_data["checks"][0]
    check["provenance"]["trusted"] = False
    provider, config = inputs(fixture_data)
    ci = assess_checks(provider.checks, provider.snapshot, config)
    assert ci["expected"][0]["evidence"]["relationship"] == "associated_only"
    assert ci["outcome"] == "incomplete"
    check["provenance"] = {
        "trusted": True,
        "tested_sha": "4" * 40,
        "parents": [fixture_data["change"]["head_sha"], fixture_data["change"]["target_sha"]],
    }
    provider, config = inputs(fixture_data)
    ci = assess_checks(provider.checks, provider.snapshot, config)
    assert ci["evidence"][0]["relationship"] == "merge_exact"
    assert ci["outcome"] == "verified"
    fixture_data["change"]["target_sha"] = "3" * 40
    provider, config = inputs(fixture_data)
    ci = assess_checks(provider.checks, provider.snapshot, config)
    assert ci["outcome"] == "incomplete"


def test_newer_failed_rerun_supersedes_success(inputs, fixture_data):
    rerun = copy.deepcopy(fixture_data["checks"][0])
    rerun.update(attempt=2, raw_state="failure", native_id="check-2")
    fixture_data["checks"].insert(0, rerun)
    provider, config = inputs(fixture_data)
    ci = assess_checks(provider.checks, provider.snapshot, config)
    assert ci["outcome"] == "failed"
    assert ci["expected"][0]["evidence"]["attempt"] == 2


def test_failed_check_does_not_hide_another_unverified_failure(
    inputs, fixture_data, policy_data, tmp_path
):
    fixture_data["checks"][0]["raw_state"] = "failure"
    other = copy.deepcopy(fixture_data["checks"][0])
    other["name"] = "lint"
    other["provenance"]["trusted"] = False
    fixture_data["checks"].append(other)
    policy_data["ci"]["expected"].append(
        {"kind": "check_run", "source_id": other["source_id"], "name": "lint"}
    )
    report, _ = run_report(inputs, tmp_path, fixture_data, policy_data)
    assert report["ci"]["outcome"] == "failed"
    assert report["status"] == "incomplete"


def test_review_only_requires_explicit_policy(inputs, fixture_data, policy_data, tmp_path):
    policy_data["ci"]["expected"] = []
    with pytest.raises(CirraError, match="review_only"):
        inputs(fixture_data, policy_data)
    policy_data["ci"]["mode"] = "review_only"
    report, _ = run_report(inputs, tmp_path, fixture_data, policy_data)
    assert report["ci"]["outcome"] == "review_only"
    assert report["status"] == "complete"


@pytest.mark.parametrize("gap", ["binary", "truncated", "limit", "collection"])
def test_incomplete_input_never_looks_like_clean_review(
    gap, inputs, fixture_data, policy_data, tmp_path
):
    fixture_data["reviewer_response"] = {"findings": [], "notes": []}
    if gap == "binary":
        fixture_data["files"][0].update(status="binary", patch=None)
    elif gap == "truncated":
        fixture_data["files"][0]["patch"] = "@@ -1,2 +1,2 @@\n def add(a, b):\n"
    elif gap == "limit":
        policy_data["limits"]["max_diff_bytes"] = 1
    else:
        fixture_data["coverage_gaps"] = ["provider page 2 inaccessible"]
    report, _ = run_report(inputs, tmp_path, fixture_data, policy_data)
    assert report["status"] == "incomplete"
    assert report["coverage"]["gaps"]


@pytest.mark.parametrize("fault", ["line", "path", "side", "json", "tool", "duplicate", "oversize"])
def test_malformed_reviews_do_not_become_zero_findings(
    fault, inputs, fixture_data, policy_data, tmp_path
):
    finding = fixture_data["reviewer_response"]["findings"][0]
    if fault == "line":
        finding["end_line"] = 999
    elif fault == "path":
        finding["path"] = "../../secrets.env"
    elif fault == "side":
        finding["side"] = "system"
    elif fault == "json":
        fixture_data["reviewer_response"] = "not json"
    elif fault == "tool":
        fixture_data["reviewer_response"]["tools"] = [{"shell": "echo unsafe"}]
    elif fault == "duplicate":
        fixture_data["reviewer_response"] = '{"findings":[],"findings":[],"notes":[]}'
    else:
        policy_data["limits"]["max_response_bytes"] = 5
    report, _ = run_report(inputs, tmp_path, fixture_data, policy_data)
    assert report["review"]["outcome"] == "malformed"
    assert report["status"] == "incomplete"
    assert "not a zero-finding review" in render_markdown(report)


def test_removed_lines_and_renames_use_captured_side(inputs, fixture_data, tmp_path):
    file = fixture_data["files"][0]
    file.update(status="renamed", old_path="old/arithmetic.py")
    finding = fixture_data["reviewer_response"]["findings"][0]
    finding.update(side="old", evidence="Removed addition behavior.")
    report, _ = run_report(inputs, tmp_path, fixture_data)
    assert report["review"]["outcome"] == "succeeded"
    assert report["review"]["findings"][0]["old_path"] == "old/arithmetic.py"


def test_valid_zero_findings_and_untrusted_text_render_safely(inputs, fixture_data, tmp_path):
    fixture_data["reviewer_response"] = {
        "findings": [],
        "notes": ["/merge @everyone <script>bad</script> [click](https://evil.invalid)"],
    }
    report, _ = run_report(inputs, tmp_path, fixture_data)
    assert report["review"]["outcome"] == "succeeded"
    assert report["status"] == "complete"
    markdown = render_markdown(report)
    assert "No findings returned" in markdown
    assert "&#64;everyone" in markdown
    for unsafe in ("/merge", "@everyone", "<script>", "[click]("):
        assert unsafe not in markdown


def test_rename_requires_original_path(inputs, fixture_data):
    fixture_data["files"][0]["status"] = "renamed"
    with pytest.raises(CirraError, match="old_path"):
        inputs(fixture_data)


@pytest.mark.parametrize("side, outcome", [("old", "succeeded"), ("new", "malformed")])
def test_deleted_file_locations_use_old_side(side, outcome, inputs, fixture_data, tmp_path):
    fixture_data["files"][0].update(
        status="removed", patch="@@ -1,2 +0,0 @@\n-def add(a, b):\n-    return a + b\n"
    )
    fixture_data["reviewer_response"]["findings"][0]["side"] = side
    report, _ = run_report(inputs, tmp_path, fixture_data)
    assert report["review"]["outcome"] == outcome


def test_reviewer_timeout_is_persisted_without_automatic_retry(inputs, tmp_path):
    class TimeoutReviewer:
        def review(self, packet):
            raise TimeoutError("do not echo sensitive transport details")

    report, _ = run_report(inputs, tmp_path, reviewer=TimeoutReviewer())
    assert report["review"]["outcome"] == "timeout"
    assert "sensitive" not in str(report)
    repeated, reused = run_report(inputs, tmp_path)
    assert reused and repeated["review"]["outcome"] == "timeout"
