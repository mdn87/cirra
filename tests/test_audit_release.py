"""Stage 5 of the production-audit tiers plan: the release gate and waivers."""

import json
import time
from dataclasses import replace

import pytest
from audit_fixtures import build_report
from test_coordination_extensions import captured

from cirra import audits, release
from cirra.audit import load_audit_policy
from cirra.coordinator import review
from cirra.domain import CirraError
from cirra.job_queue import AuditQueue
from cirra.store import Store


@pytest.fixture
def candidate(inputs, fixture_data, tmp_path):
    """A reviewed, release-ready candidate and a policy with a release audit gate."""
    fixture_data["reviewer_response"] = {"findings": [], "notes": []}
    provider, config = captured(inputs, fixture_data)
    provider.checks[0]["provenance"]["definition_digest"] = "d" * 64
    config = replace(
        config,
        expected_checks=tuple(
            {**item, "definition_digest": "d" * 64} for item in config.expected_checks
        ),
    )
    store = Store(tmp_path / "state")
    report, _ = review(provider, config, store)
    gated = replace(
        config, audit=load_audit_policy({"tiers": ["pr", "release"], "gates": {"release": "high"}})
    )
    return provider, config, gated, store, report


def store_release_audit(store, policy, report, gaps=()):
    snapshot = report["snapshot"]
    repository = {key: snapshot[key] for key in ("provider", "instance", "repository_id")}
    identity = audits.audit_identity(policy, "release", repository, snapshot["head_sha"])
    attempt, _ = audits.begin(store, identity, "release:main")
    body = build_report("release", snapshot["head_sha"], gaps=gaps)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE audits SET state='complete', completed_at=?, report_json=?, "
            "comparison_json=? WHERE attempt_id=?",
            (
                time.time(),
                json.dumps(body),
                json.dumps({"new": [], "open": [], "resolved": []}),
                attempt["attempt_id"],
            ),
        )
    return attempt["attempt_id"]


class HTTP:
    """Provider reads for a merged candidate on a protected branch; records dispatches."""

    def __init__(self, provider):
        self.provider, self.writes = provider, 0

    def request(self, method, route, params=None, body=None):
        head = self.provider.snapshot.head_sha
        if method == "POST":
            self.writes += 1
            return {}, False
        if "/pulls/" in route:
            return {
                "merged": True,
                "head": {"sha": head},
                "base": {"ref": "main", "repo": {"id": self.provider.snapshot.repository_id}},
            }, False
        if "/branches/" in route:
            return {"protected": True, "commit": {"sha": head}}, False
        if "/compare/" in route:
            return {"status": "identical", "merge_base_commit": {"sha": head}}, False
        return {"workflow_runs": []}, False


def test_policy_without_release_tier_leaves_planning_unchanged(candidate):
    _, config, _, store, report = candidate
    assert release.audit_gate(store, config, report, "main") is None
    pr_only = replace(config, audit=load_audit_policy({"tiers": ["pr"]}))
    assert release.audit_gate(store, pr_only, report, "main") is None
    assert "audit_gate" not in release.plan(report, 12, "main")


def test_missing_release_audit_refuses_and_queues_one(candidate):
    _, _, gated, store, report = candidate
    with pytest.raises(CirraError, match="is not complete"):
        release.audit_gate(store, gated, report, "main")
    with pytest.raises(CirraError, match="is not complete"):
        release.audit_gate(store, gated, report, "main")
    [job] = AuditQueue(store.path.parent).jobs()
    assert job["tier"] == "release" and job["scope"] == "release:main"
    assert job["identity"]["head"] == report["snapshot"]["head_sha"]
    assert job["context"] == {"route": None, "change": None, "ref": "refs/heads/main"}


def test_blocking_finding_refuses_and_a_clean_audit_passes(candidate):
    _, _, gated, store, report = candidate
    store_release_audit(store, gated.audit, report, [("C064", "README.md", 1, 1, "high")])
    with pytest.raises(CirraError, match=r"1 finding\(s\) at or above high: C064:README.md"):
        release.audit_gate(store, gated, report, "main")
    lenient = replace(
        gated,
        audit=load_audit_policy({"tiers": ["pr", "release"], "gates": {"release": "critical"}}),
    )
    store_release_audit(store, lenient.audit, report, [("C064", "README.md", 1, 1, "high")])
    gate = release.audit_gate(store, lenient, report, "main")
    assert gate["state"] == "passed" and gate["blocking"] == []
    request = release.plan(report, 12, "main", audit=gate)
    assert request["audit_gate"] == gate


def test_waiver_is_the_only_override_and_records_its_reason(candidate):
    _, _, gated, store, report = candidate
    store_release_audit(store, gated.audit, report, [("C064", "README.md", 1, 1, "high")])
    gate = release.audit_gate(store, gated, report, "main", waiver="Accepted for the hotfix")
    assert gate["state"] == "waived" and gate["gate"] == "block"
    assert gate["reason"] == "Accepted for the hotfix" and gate["blocking"] == ["C064:README.md"]
    with pytest.raises(CirraError, match="nonempty string"):
        release.audit_gate(store, gated, report, "main", waiver="")


def test_dispatch_rechecks_the_gate(candidate):
    provider, _, gated, store, report = candidate
    ungated = release.plan(report, 12, "main")
    with pytest.raises(CirraError, match="is not complete"):
        release.dispatch(ungated, store, gated, lambda: provider, HTTP(provider))
    store_release_audit(store, gated.audit, report)
    with pytest.raises(CirraError, match="predates the audit gate"):
        release.dispatch(ungated, store, gated, lambda: provider, HTTP(provider))
    request = release.plan(
        report, 12, "main", audit=release.audit_gate(store, gated, report, "main")
    )
    http = HTTP(provider)
    assert release.dispatch(request, store, gated, lambda: provider, http)["state"] == "accepted"
    assert http.writes == 1


def test_dispatch_honours_a_recorded_waiver(candidate):
    provider, _, gated, store, report = candidate
    store_release_audit(store, gated.audit, report, [("C064", "README.md", 1, 1, "high")])
    waived = release.audit_gate(store, gated, report, "main", waiver="Known; tracked in #99")
    request = release.plan(report, 12, "main", audit=waived)
    http = HTTP(provider)
    assert release.dispatch(request, store, gated, lambda: provider, http)["state"] == "accepted"


def test_plan_release_cli_writes_the_waiver_receipt(candidate, tmp_path, policy_data, capsys):
    import yaml

    from cirra.cli import main

    _, _, gated, store, report = candidate
    store_release_audit(store, gated.audit, report, [("C064", "README.md", 1, 1, "high")])
    policy = {**policy_data, "audit": {"tiers": ["pr", "release"], "gates": {"release": "high"}}}
    path = tmp_path / "gated.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    common = ["plan-release", "--run", report["run_id"], "--workflow-id", "12", "--ref", "main"]
    common += ["--output", str(tmp_path / "request.json"), "--state-dir", str(store.path.parent)]
    assert main([*common, "--config", str(path)]) == 2
    assert "at or above high" in capsys.readouterr().err
    assert main([*common, "--waive-audit", "reason"]) == 2
    assert "needs the --config" in capsys.readouterr().err
    assert main([*common, "--config", str(path), "--waive-audit", "Accepted risk"]) == 0
    request = json.loads((tmp_path / "request.json").read_text(encoding="utf-8"))
    assert request["audit_gate"]["state"] == "waived"
    receipt = store.path.parent / "audit-waivers" / f"{request['request_id']}.json"
    assert json.loads(receipt.read_text(encoding="utf-8"))["reason"] == "Accepted risk"
