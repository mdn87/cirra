import copy
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_coordination_extensions import Publisher, captured

from cirra import events, release
from cirra.coordinator import review
from cirra.domain import CirraError
from cirra.fixture import FixtureProvider
from cirra.policy import assess_checks
from cirra.publish import GitHubPublisher, github_app_token, marker, publish
from cirra.store import Store
from cirra.transport import RemoteUncertain


def test_slow_publisher_does_not_block_another_changes_review(inputs, fixture_data, tmp_path):
    provider, config = captured(inputs, fixture_data)
    config = replace(config, claim_ttl_seconds=10, wait_seconds=15)
    store = Store(tmp_path / "state")
    report, _ = review(provider, config, store)
    other_data = copy.deepcopy(fixture_data)
    other_data["change"]["change_id"] = "another-change"
    other = FixtureProvider.from_data(other_data, config)
    entered, unblock = threading.Event(), threading.Event()

    class SlowPublisher(Publisher):
        def find(self, report, slot):
            entered.set()
            assert unblock.wait(30)
            return []

    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = workers.submit(
            publish, store, config, report["run_id"], lambda: provider, SlowPublisher()
        )
        try:
            if not entered.wait(15):
                # Surface a worker exception instead of hiding it as a barrier timeout.
                pending.result(timeout=1)
                pytest.fail("publisher did not reach the network barrier")
            second = workers.submit(review, other, config, store)
            assert second.result(timeout=10)[0]["review"]["outcome"] == "succeeded"
        finally:
            unblock.set()
        assert pending.result(timeout=15)["state"] == "published"


def test_uncertain_patch_retries_known_id_without_claiming_duplicate_create_risk(
    inputs, fixture_data, tmp_path
):
    provider, config = captured(inputs, fixture_data)
    store, writer = Store(tmp_path / "state"), Publisher()
    report, _ = review(provider, config, store)
    first = publish(store, config, report["run_id"], lambda: provider, writer)

    def lost(report, metadata, identifier):
        raise RemoteUncertain("unconfirmed update")

    writer.write = lost
    assert (
        publish(store, config, report["run_id"], lambda: provider, writer)["state"] == "uncertain"
    )
    receipt = publish(store, config, report["run_id"], lambda: provider, writer, reconcile=True)
    assert "Known-object" in receipt["reason"]
    del writer.write
    receipt = publish(
        store, config, report["run_id"], lambda: provider, writer, retry_uncertain=True
    )
    assert receipt["state"] == "published" and receipt["native_id"] == first["native_id"]
    assert not receipt["duplicate_risk_accepted"]


def test_definitive_refusal_allows_retry_and_oversize_never_writes_intent(
    inputs, fixture_data, tmp_path
):
    provider, config = captured(inputs, fixture_data)
    store, writer = Store(tmp_path / "state"), Publisher()
    report, _ = review(provider, config, store)

    def refused(report, metadata, identifier):
        raise CirraError("provider refused request (HTTP 422)")

    writer.write = refused
    assert publish(store, config, report["run_id"], lambda: provider, writer)["state"] == "failed"
    del writer.write
    assert (
        publish(store, config, report["run_id"], lambda: provider, writer)["state"] == "published"
    )
    with store.connection() as connection:
        before = connection.execute(
            "SELECT body FROM reports ORDER BY sequence DESC LIMIT 1"
        ).fetchone()[0]
    huge = json.loads(before)
    huge["review"]["notes"] = ["x" * 61000]
    with store.lease(provider.snapshot.change_key, 10, 2) as claim:
        huge["report_digest"] = "huge-test-report"
        store.save_report(claim, huge)
    writes = writer.writes
    with pytest.raises(CirraError, match="60 KiB"):
        publish(store, config, report["run_id"], lambda: provider, writer)
    assert writer.writes == writes


def test_canonical_choice_persists_and_missing_object_can_be_recreated_explicitly(
    inputs, fixture_data, tmp_path
):
    provider, config = captured(inputs, fixture_data)
    store, writer = Store(tmp_path / "state"), Publisher()
    report, _ = review(provider, config, store)
    publish(store, config, report["run_id"], lambda: provider, writer)
    writer.objects.append({**copy.deepcopy(writer.objects[0]), "id": "999"})
    with pytest.raises(CirraError, match="multiple"):
        publish(store, config, report["run_id"], lambda: provider, writer)
    receipt = publish(store, config, report["run_id"], lambda: provider, writer, canonical_id="999")
    writer.objects.append({**copy.deepcopy(writer.objects[0]), "id": "888"})
    assert publish(store, config, report["run_id"], lambda: provider, writer)["native_id"] == "999"
    assert receipt["canonical_id"] == "999"
    writer.objects = []
    assert (
        publish(store, config, report["run_id"], lambda: provider, writer, reconcile=True)["state"]
        == "uncertain"
    )
    receipt = publish(
        store, config, report["run_id"], lambda: provider, writer, retry_uncertain=True
    )
    assert receipt["state"] == "published" and receipt["duplicate_risk_accepted"]
    assert receipt["canonical_id"] is None


def test_gitlab_maintains_one_note_across_head_changes(inputs, fixture_data, tmp_path):
    provider, config = captured(inputs, fixture_data)
    provider.snapshot = replace(provider.snapshot, provider="gitlab", instance="https://gitlab.com")
    store, writer = Store(tmp_path / "state"), Publisher()
    first, _ = review(provider, config, store)
    one = publish(store, config, first["run_id"], lambda: provider, writer)
    provider.snapshot = replace(provider.snapshot, head_sha="a" * 40)
    second, _ = review(provider, config, store)
    two = publish(store, config, second["run_id"], lambda: provider, writer)
    assert (
        one["native_id"] == two["native_id"] and one["metadata"]["slot"] == two["metadata"]["slot"]
    )


def test_changed_delivery_header_does_not_bypass_authenticated_payload_dedup(tmp_path):
    hint = {
        "provider": "github",
        "instance": "https://github.com",
        "delivery": "one",
        "payload_digest": "same-body",
    }
    calls = []

    def capture():
        calls.append(True)
        return tmp_path / "snapshot.json"

    store = Store(tmp_path / "state")
    events.ingest(store, hint, capture)
    assert events.ingest(store, {**hint, "delivery": "two"}, capture)["duplicate"]
    assert len(calls) == 1


@pytest.mark.parametrize("widen", ["permissions", "repository"])
def test_app_token_rejects_wider_server_grants(monkeypatch, widen):
    monkeypatch.setitem(sys.modules, "jwt", SimpleNamespace(encode=lambda *a, **kw: "signed-jwt"))

    class HTTP:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, route, params=None, body=None):
            if method == "GET":
                return {"app_id": 42}, False
            result = {
                "token": "synthetic-token",
                "permissions": dict(body["permissions"]),
                "repositories": [{"id": 123}],
            }
            if widen == "permissions":
                result["permissions"]["contents"] = "write"
            else:
                result["repositories"].append({"id": 456})
            return result, False

    monkeypatch.setattr("cirra.publish.JSONClient", HTTP)
    with pytest.raises(CirraError, match="restricted|exact"):
        github_app_token(42, 7, "key", 123)


def test_github_find_ignores_foreign_app_and_rejects_malformed_write(inputs, tmp_path):
    provider, config = inputs()
    report, _ = review(provider, config, Store(tmp_path / "state"))

    class HTTP:
        def request(self, method, route, params=None, body=None):
            if method == "POST":
                return {"app": None}, False
            common = {
                "head_sha": provider.snapshot.head_sha,
                "external_id": "slot",
                "name": "cirra/review",
                "output": {"summary": marker({"slot": "slot"})},
            }
            return {
                "check_runs": [
                    {**common, "id": 1, "app": {"id": 999}},
                    {**common, "id": 2, "app": {"id": 42}},
                ]
            }, False

    publisher = GitHubPublisher("owner/repo", 42, "token", http=HTTP())
    assert [item["id"] for item in publisher.find(report, "slot")] == ["2"]
    with pytest.raises(RemoteUncertain, match="malformed"):
        publisher.write(report, {"slot": "slot"})


def test_ci_definition_mismatch_never_satisfies_release_evidence(inputs):
    provider, config = inputs()
    config = replace(
        config,
        expected_checks=tuple(
            {**item, "definition_digest": "d" * 64} for item in config.expected_checks
        ),
    )
    assert assess_checks(provider.checks, provider.snapshot, config)["outcome"] == "incomplete"
    provider.checks[0]["provenance"]["definition_digest"] = "d" * 64
    assert assess_checks(provider.checks, provider.snapshot, config)["outcome"] == "verified"


@pytest.mark.parametrize("condition", ["accepted", "unmerged", "unprotected"])
def test_release_uses_protected_target_and_never_retries_acknowledged_dispatch(
    inputs, fixture_data, tmp_path, condition
):
    fixture_data["reviewer_response"] = {"findings": [], "notes": []}
    fixture_data["checks"][0]["provenance"]["definition_digest"] = "d" * 64
    provider, config = captured(inputs, fixture_data)
    config = replace(
        config,
        expected_checks=tuple(
            {**item, "definition_digest": "d" * 64} for item in config.expected_checks
        ),
    )
    store = Store(tmp_path / "state")
    report, _ = review(provider, config, store)
    request = release.plan(report, 12, "main")
    assert release.plan(report, 12, "main")["request_id"] == request["request_id"]

    class HTTP:
        writes = 0

        def request(self, method, route, params=None, body=None):
            if method == "POST":
                self.writes += 1
                assert body["ref"] == "main"
                return None, False
            if "/pulls/" in route:
                return {
                    "merged": condition != "unmerged",
                    "head": {"sha": provider.snapshot.head_sha},
                    "base": {"ref": "main", "repo": {"id": provider.snapshot.repository_id}},
                }, False
            if "/branches/" in route:
                return {"protected": condition != "unprotected", "commit": {"sha": "a" * 40}}, False
            if "/compare/" in route:
                return {
                    "status": "ahead",
                    "merge_base_commit": {"sha": provider.snapshot.head_sha},
                }, False
            return {"workflow_runs": []}, False

    http = HTTP()
    if condition != "accepted":
        with pytest.raises(CirraError, match="merged|protected"):
            release.dispatch(request, store, config, lambda: provider, http)
        assert http.writes == 0
    else:
        one = release.dispatch(request, store, config, lambda: provider, http)
        two = release.dispatch(request, store, config, lambda: provider, http, retry_uncertain=True)
        assert one["state"] == two["state"] == "accepted" and http.writes == 1
        assert one["workflow_sha"] == "a" * 40
