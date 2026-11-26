import copy
import io
import json
import traceback
from dataclasses import replace
from email.message import Message
from itertools import pairwise
from urllib.error import URLError
from urllib.request import HTTPSHandler, build_opener
from urllib.response import addinfourl

import pytest

from cirra import provenance, release
from cirra.cli import main
from cirra.config import MergeGate
from cirra.domain import CirraError, ProviderIdentityMismatch, digest
from cirra.fixture import FixtureProvider
from cirra.github import GitHubClient, GitHubError, NoRedirect, checks_from, collect, export_capture
from cirra.policy import assess_checks
from cirra.session import import_response, prepare
from cirra.store import Store
from cirra.transport import ReadUnavailable

HEAD, BASE, TARGET = "2" * 40, "1" * 40, "3" * 40
TIME = "2026-10-15T06:00:00Z"


def check(identifier=8, state="success"):
    return {
        "id": identifier,
        "name": "test",
        "app": {"id": 42},
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": state,
        "started_at": TIME,
        "html_url": "https://github.com/example/repo/actions/runs/7",
        "check_suite": {"id": 7},
    }


class API:
    def __init__(self):
        self.calls = []
        self.pr = {
            "number": 9,
            "head": {"sha": HEAD},
            "base": {"sha": TARGET, "ref": "main", "repo": {"id": 123}},
            "changed_files": 1,
        }
        self.comparison = {
            "base_commit": {"sha": TARGET},
            "merge_base_commit": {"sha": BASE},
            "files": [
                {
                    "filename": "src/add.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 1,
                    "patch": "@@ -1 +1 @@\n-return a + b\n+return a - b\n",
                }
            ],
        }
        self.checks = [check()]
        self.statuses = [
            {
                "id": 12,
                "context": "legacy",
                "state": "error",
                "creator": {"id": 55},
                "created_at": TIME,
                "url": "https://api.github.com/status/12",
            }
        ]
        self.overrides = {}
        self.pr_sequence = []
        self.branch = {"name": "main", "commit": {"sha": TARGET}}
        self.branch_sequence = []

    def get(self, route="", params=None):
        params = params or {}
        self.calls.append((route, params))
        key = (route, params.get("page", 1))
        if key in self.overrides:
            value = self.overrides[key]
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)
        if not route:
            data = {"id": 123, "full_name": "example/repo"}
        elif route == "pulls/9":
            data = self.pr_sequence.pop(0) if self.pr_sequence else self.pr
        elif route.startswith("branches/"):
            data = self.branch_sequence.pop(0) if self.branch_sequence else self.branch
        elif route.startswith("compare/"):
            data = self.comparison
        elif route.endswith("check-runs"):
            data = {"total_count": len(self.checks), "check_runs": self.checks}
        elif route.endswith("statuses"):
            data = self.statuses
        else:
            raise AssertionError(route)
        return copy.deepcopy(data), False


@pytest.fixture
def github_policy(inputs, policy_data, fixture_data):
    policy_data["repositories"][0]["repository_id"] = "123"
    policy_data["reviewer"].update(kind="session", endpoint="session://manual")
    policy_data["ci"]["expected"][0]["source_id"] = "42"
    fixture_data["change"]["repository_id"] = "123"
    return inputs(data=fixture_data, policy=policy_data)[1]


def test_pinned_live_snapshot_preserves_native_results_without_claiming_checkout(github_policy):
    api = API()
    data = collect("example/repo", 9, github_policy, client=api)
    provider = FixtureProvider.from_data(data, github_policy)
    assert provider.snapshot.head_sha == HEAD
    assert provider.snapshot.target_sha == TARGET
    assert provider.snapshot.diff_base_sha == BASE
    assert provider.snapshot.coverage_gaps == ()
    assert api.calls[3] == (f"compare/{TARGET}...{HEAD}", {"per_page": 1, "page": 1})
    assert api.calls[-2][0] == "pulls/9"
    assert api.calls[-1][0] == "branches/main"
    assert provider.checks[0]["run_id"] is None
    assert provider.checks[0]["attempt"] is None
    assert provider.checks[0]["check_suite_id"] == "7"
    assert provider.checks[1]["raw_state"] == "error"
    assert provider.checks[1]["normalized_state"] == "failure"
    ci = assess_checks(provider.checks, provider.snapshot, github_policy)
    assert ci["outcome"] == "incomplete"
    assert ci["expected"][0]["state"] == "success"
    assert ci["expected"][0]["evidence"]["relationship"] == "associated_only"


def test_allowlist_failure_stops_before_reading_pull_request(github_policy):
    api = API()
    api.overrides[("", 1)] = ({"id": 999, "full_name": "example/repo"}, False)
    with pytest.raises(CirraError, match="allowlist"):
        collect("example/repo", 9, github_policy, client=api)
    assert len(api.calls) == 1


def test_live_target_branch_overrides_stale_pr_base_metadata(github_policy):
    api = API()
    api.pr["base"]["sha"] = BASE
    stale_comparison = copy.deepcopy(api.comparison)
    stale_comparison["base_commit"]["sha"] = BASE
    api.overrides[(f"compare/{BASE}...{HEAD}", 1)] = (stale_comparison, False)
    data = collect("example/repo", 9, github_policy, client=api)
    assert data["change"]["target_sha"] == TARGET
    assert (f"compare/{TARGET}...{HEAD}", {"per_page": 1, "page": 1}) in api.calls


def test_target_branch_advance_retries_even_when_pr_metadata_does_not_change(github_policy):
    api = API()
    old_branch = {"name": "main", "commit": {"sha": BASE}}
    api.branch_sequence = [old_branch, api.branch, api.branch, api.branch]
    old_comparison = copy.deepcopy(api.comparison)
    old_comparison["base_commit"]["sha"] = BASE
    api.overrides[(f"compare/{BASE}...{HEAD}", 1)] = (old_comparison, False)
    data = collect("example/repo", 9, github_policy, client=api)
    assert data["change"]["target_sha"] == TARGET
    assert len([route for route, _ in api.calls if route.startswith("compare/")]) == 2


def test_head_change_during_collection_retries_snapshot(github_policy):
    api = API()
    old = copy.deepcopy(api.pr)
    old["head"]["sha"] = "a" * 40
    api.pr_sequence = [old, api.pr, api.pr, api.pr]
    data = collect("example/repo", 9, github_policy, client=api)
    assert data["change"]["head_sha"] == HEAD
    assert data["change"]["target_sha"] == TARGET
    assert len([route for route, _ in api.calls if route.startswith("compare/")]) == 2


@pytest.mark.parametrize("failure", ["denied", "wrong-name", "missing-commit"])
def test_target_branch_failure_never_falls_back_to_pr_metadata(failure, github_policy):
    api = API()
    if failure == "denied":
        api.overrides[("branches/main", 1)] = GitHubError("GitHub access denied (HTTP 403)")
    elif failure == "wrong-name":
        api.branch["name"] = "other"
    else:
        api.branch.pop("commit")
    with pytest.raises(CirraError):
        collect("example/repo", 9, github_policy, client=api)
    assert not any(route.startswith("compare/") for route, _ in api.calls)


def test_fork_head_does_not_choose_target_repository_or_branch(github_policy):
    api = API()
    api.pr["head"].update(ref="untrusted-branch", repo={"id": 999, "full_name": "other/fork"})
    api.pr["base"]["ref"] = "release/one"
    api.branch["name"] = "release/one"
    data = collect("example/repo", 9, github_policy, client=api)
    assert data["change"]["repository_id"] == "123"
    assert data["change"]["target_sha"] == TARGET
    assert [route for route, _ in api.calls if route.startswith("branches/")] == [
        "branches/release%2Fone",
        "branches/release%2Fone",
    ]


def test_continuously_changing_snapshot_fails_without_export(github_policy):
    api = API()
    old = copy.deepcopy(api.pr)
    old["head"]["sha"] = "a" * 40
    api.pr_sequence = [old, api.pr] * 3
    with pytest.raises(CirraError, match="changed during collection"):
        collect("example/repo", 9, github_policy, client=api)


@pytest.mark.parametrize("condition", ["count", "missing", "truncated", "binary"])
def test_missing_patch_coverage_is_visible(condition, github_policy):
    api = API()
    if condition == "count":
        api.pr["changed_files"] = 400
    elif condition == "missing":
        api.comparison["files"][0].pop("patch")
    elif condition == "truncated":
        api.comparison["files"][0]["additions"] = 20
    else:
        api.comparison["files"][0].update(patch=None, additions=0, deletions=0)
    data = collect("example/repo", 9, github_policy, client=api)
    assert FixtureProvider.from_data(data, github_policy).snapshot.coverage_gaps


def test_check_pagination_and_newest_rerun(github_policy):
    api = API()
    later = check(10, "timed_out")
    later["started_at"] = "2026-10-15T07:00:00Z"
    api.overrides[(f"commits/{HEAD}/check-runs", 1)] = (
        {"total_count": 2, "check_runs": [check()]},
        True,
    )
    api.overrides[(f"commits/{HEAD}/check-runs", 2)] = (
        {"total_count": 2, "check_runs": [later]},
        False,
    )
    provider = FixtureProvider.from_data(
        collect("example/repo", 9, github_policy, client=api), github_policy
    )
    ci = assess_checks(provider.checks, provider.snapshot, github_policy)
    assert ci["expected"][0]["state"] == "failure"
    assert ci["expected"][0]["evidence"]["raw_state"] == "timed_out"
    assert len(provider.checks) == 3
    assert api.calls[4][1]["filter"] == "all"


@pytest.mark.parametrize("failure", ["outage", "limit", "duplicate", "missing-page"])
def test_incomplete_check_collection_cannot_be_hidden(failure, github_policy, tmp_path):
    api = API()
    route = f"commits/{HEAD}/check-runs"
    api.overrides[(route, 1)] = ({"total_count": 2, "check_runs": [check()]}, True)
    if failure == "outage":
        api.overrides[(route, 2)] = GitHubError("GitHub check request failed (HTTP 503)")
    elif failure == "duplicate":
        api.overrides[(route, 2)] = ({"total_count": 2, "check_runs": [check()]}, False)
    elif failure == "missing-page":
        api.overrides[(route, 1)] = ({"total_count": 2, "check_runs": [check()]}, False)
    data = collect(
        "example/repo", 9, github_policy, client=api, max_pages=1 if failure == "limit" else 10
    )
    provider = FixtureProvider.from_data(data, github_policy)
    assert provider.check_errors
    _, report, _ = prepare(provider, github_policy, Store(tmp_path / "state"))
    assert report["ci"]["collection_errors"]
    assert report["status"] == "incomplete"
    assert report["collection"]["source"] == "github_rest"


def test_collection_errors_do_not_invalidate_review_packet(github_policy, tmp_path):
    api = API()
    store = Store(tmp_path / "state")
    provider = FixtureProvider.from_data(
        collect("example/repo", 9, github_policy, client=api), github_policy
    )
    packet, _, _ = prepare(provider, github_policy, store)
    api.overrides[(f"commits/{HEAD}/statuses", 1)] = GitHubError("GitHub access denied (HTTP 403)")
    provider = FixtureProvider.from_data(
        collect("example/repo", 9, github_policy, client=api), github_policy
    )
    same, report, reused = prepare(provider, github_policy, store)
    assert same == packet and reused is True
    assert report["ci"]["collection_errors"]


def test_ci_outage_keeps_completed_review_incomplete(github_policy, tmp_path):
    policy = replace(github_policy, ci_mode="review_only", expected_checks=())
    api = API()
    api.overrides[(f"commits/{HEAD}/statuses", 1)] = GitHubError("GitHub access denied (HTTP 403)")
    provider = FixtureProvider.from_data(collect("example/repo", 9, policy, client=api), policy)
    store = Store(tmp_path / "state")
    packet, _, _ = prepare(provider, policy, store)
    response = {
        "schema_version": 1,
        "run_id": packet["run_id"],
        "packet_digest": packet["packet_digest"],
        "session": {"tool": "test", "id": "test-session", "model": "test-model"},
        "review": {"findings": [], "notes": []},
    }
    response_path = tmp_path / "response.json"
    response_path.write_text(json.dumps(response), encoding="utf-8")
    report, _ = import_response(provider, policy, store, packet["run_id"], response_path)
    assert report["review"]["outcome"] == "succeeded"
    assert report["ci"]["outcome"] == "incomplete" and report["status"] == "incomplete"


def test_missing_start_time_cannot_hide_a_competing_run(github_policy):
    api = API()
    pending = check(13)
    pending.update(status="queued", conclusion=None, started_at=None)
    api.checks.append(pending)
    provider = FixtureProvider.from_data(
        collect("example/repo", 9, github_policy, client=api), github_policy
    )
    expected = assess_checks(provider.checks, provider.snapshot, github_policy)["expected"][0]
    assert expected["state"] == "unknown" and expected["verified_revision"] is False


def test_unknown_and_queued_states_and_unrelated_sha(github_policy):
    api = API()
    api.checks[0].update(status="queued", conclusion=None, started_at=None)
    data = collect("example/repo", 9, github_policy, client=api)
    provider = FixtureProvider.from_data(data, github_policy)
    ci = assess_checks(provider.checks, provider.snapshot, github_policy)
    assert ci["expected"][0]["state"] == "pending"
    assert ci["expected"][0]["evidence"]["started_at"] is None
    api.checks[0].update(status="completed", conclusion=None)
    provider = FixtureProvider.from_data(
        collect("example/repo", 9, github_policy, client=api), github_policy
    )
    assert provider.checks[0]["raw_state"] == "completed"
    assert provider.checks[0]["normalized_state"] == "unknown"
    api.checks[0].update(status="completed", conclusion="future_state", head_sha="f" * 40)
    provider = FixtureProvider.from_data(
        collect("example/repo", 9, github_policy, client=api), github_policy
    )
    evidence = assess_checks(provider.checks, provider.snapshot, github_policy)["evidence"][0]
    assert evidence["state"] == "unknown" and evidence["relationship"] == "unrelated"


def test_saved_capture_is_usable_by_existing_session_commands(github_policy, tmp_path):
    data = collect("example/repo", 9, github_policy, client=API())
    path = export_capture(data, tmp_path)
    provider = FixtureProvider(path, github_policy)
    assert provider.collection == data["collection"]
    assert digest(provider.packet())


class Transport(HTTPSHandler):
    def __init__(self, responses):
        super().__init__()
        self.responses, self.requests = list(responses), []

    def https_open(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        status, body, headers = response
        message = Message()
        for name, value in headers.items():
            message[name] = value
        result = addinfourl(io.BytesIO(body), message, request.full_url, status)
        result.msg = "test response"
        return result


def client_with(transport, **kwargs):
    return GitHubClient("example/repo", opener=build_opener(NoRedirect(), transport), **kwargs)


def test_transport_only_gets_fixed_github_urls_and_handles_pagination():
    transport = Transport([(200, b'{"id":123}', {"Link": '<https://other.test/leak>; rel="next"'})])
    data, more = client_with(transport, token="canary-secret").get("pulls/9", {"page": 1})
    request = transport.requests[0]
    assert request.get_method() == "GET"
    assert request.full_url == "https://api.github.com/repos/example/repo/pulls/9?page=1"
    assert request.get_header("Authorization") == "Bearer canary-secret"
    assert data == {"id": 123} and more is True
    assert len(transport.requests) == 1


def test_transport_encodes_target_branch_as_one_path_component():
    transport = Transport([(200, b"{}", {})])
    client_with(transport).get("branches/release%2Fone")
    assert transport.requests[0].full_url == (
        "https://api.github.com/repos/example/repo/branches/release%2Fone"
    )


def test_transport_reads_only_the_named_pull_merge_ref_and_pinned_commit():
    transport = Transport([(200, b"{}", {}), (200, b"{}", {})])
    client = client_with(transport)
    paths = ["git/ref/pull/7/merge", "git/commits/" + "a" * 40]
    for path in paths:
        client.get(path)
    assert [req.full_url for req in transport.requests] == [
        "https://api.github.com/repos/example/repo/" + path for path in paths
    ]
    for path in ("git/ref/heads/main", "git/ref/pull/7/head", "git/commits/main"):
        with pytest.raises(CirraError, match="unsupported"):
            client.get(path)


def test_gate_reviews_changes_that_only_appear_against_an_intermediate_main(github_policy):
    api = API()
    mid = "4" * 40
    api.comparison.update(
        merge_base_commit={"sha": TARGET},
        total_commits=2,
        commits=[
            {"sha": mid, "parents": [{"sha": TARGET}]},
            {"sha": HEAD, "parents": [{"sha": mid}]},
        ],
    )
    shifted = copy.deepcopy(api.comparison["files"][0])
    shifted["patch"] = "@@ -1 +1 @@\n-return a * b\n+return a - b\n"
    api.overrides[(f"compare/{mid}...{HEAD}", 1)] = (
        {
            "base_commit": {"sha": mid},
            "merge_base_commit": {"sha": mid},
            "files": [shifted],
        },
        False,
    )
    config = replace(github_policy, merge_gate=MergeGate("main", "high"))
    result = collect("example/repo", 9, config, client=api)
    packet = FixtureProvider.from_data(result, config).packet()
    assert packet["additional_comparisons"][0]["base_sha"] == mid
    assert packet["additional_comparisons"][0]["patches"][0]["patch"] == shifted["patch"]
    primary = copy.deepcopy(api.comparison["files"][0])
    api.comparison["files"].append({**primary, "filename": "src/second.py"})
    api.pr["changed_files"] = 2
    api.overrides[(f"compare/{mid}...{HEAD}", 1)][0]["files"] = [primary]
    subset = FixtureProvider.from_data(collect("example/repo", 9, config, client=api), config)
    assert [p["path"] for p in subset.packet()["additional_comparisons"][0]["patches"]] == [
        "src/add.py"
    ]
    api.comparison["commits"].pop(0)
    incomplete = FixtureProvider.from_data(collect("example/repo", 9, config, client=api), config)
    assert "GitHub returned incomplete PR commit history" in incomplete.snapshot.coverage_gaps


@pytest.mark.parametrize(
    ("intermediates", "move_head", "expected_reads", "complete"),
    [(2, False, 2, True), (3, False, 0, False), (2, True, 2, False)],
)
def test_intermediate_compare_budget_covers_collection_retries(
    github_policy, intermediates, move_head, expected_reads, complete
):
    api = API()
    bases = [f"{i:040x}" for i in range(1, intermediates + 1)]
    old_head = "5" * 40
    if move_head:
        api.pr_sequence = [{**api.pr, "head": {"sha": old_head}}, api.pr]
    for head in [old_head, HEAD] if move_head else [HEAD]:
        chain = [TARGET, *bases, head]
        api.overrides[(f"compare/{TARGET}...{head}", 1)] = (
            {
                **api.comparison,
                "merge_base_commit": {"sha": TARGET},
                "total_commits": len(chain) - 1,
                "commits": [
                    {"sha": child, "parents": [{"sha": parent}]}
                    for parent, child in pairwise(chain)
                ],
            },
            False,
        )
        for base in bases:
            api.overrides[(f"compare/{base}...{head}", 1)] = (
                {
                    **api.comparison,
                    "base_commit": {"sha": base},
                    "merge_base_commit": {"sha": base},
                },
                False,
            )
    gate = MergeGate("main", "high", max_intermediate_comparisons=2)
    policy = replace(github_policy, merge_gate=gate)
    captured = collect("example/repo", 9, policy, client=api)
    intermediate_routes = {f"compare/{base}" for base in bases}
    extra_reads = [route for route, _ in api.calls if route.split("...")[0] in intermediate_routes]
    assert len(extra_reads) == expected_reads
    assert captured["additional_comparisons"] == []  # Every alternate had the same patch.
    if complete:
        assert not captured["coverage_gaps"]
    else:
        assert any("intermediate comparison limit" in gap for gap in captured["coverage_gaps"])


def test_merged_release_refresh_preserves_the_reviewed_diff_and_reads_current_checks(github_policy):
    api = API()
    initial = collect("example/repo", 9, github_policy, client=api)
    expected = FixtureProvider.from_data(initial, github_policy).snapshot.record()
    api.branch["commit"]["sha"] = "5" * 40
    api.pr["merged"] = True
    api.checks[0]["conclusion"] = "failure"
    refreshed = collect("example/repo", 9, github_policy, client=api, reviewed_target=TARGET)
    assert FixtureProvider.from_data(refreshed, github_policy).snapshot.record() == expected
    check_run = next(check for check in refreshed["checks"] if check["kind"] == "check_run")
    assert check_run["normalized_state"] == "failure"
    api.pr["merged"] = False
    with pytest.raises(CirraError, match="only a merged pull request"):
        collect("example/repo", 9, github_policy, client=api, reviewed_target=TARGET)


def test_dispatch_cli_rechecks_signed_ci_after_the_target_advances(
    github_policy, inputs, policy_data, tmp_path, monkeypatch, capsys
):
    api = API()
    definition = "d" * 64
    policy_data["ci"]["expected"][0]["definition_digest"] = definition
    _, policy = inputs(policy=policy_data)
    raw = collect("example/repo", 9, policy, client=api)
    witness_key = "test-witness-key-for-the-merged-release"
    statement = {
        "version": 1,
        "witness": "test observer",
        "observed_at": TIME,
        "checks": [
            {
                "binding": provenance.binding(raw["change"], raw["checks"][0]),
                "tested_sha": HEAD,
                "parents": [],
                "definition_digest": definition,
                "evidence": "checkout recorded by test observer",
            }
        ],
    }
    receipt = tmp_path / "witness.json"
    receipt.write_text(json.dumps(provenance.sign(statement, witness_key)))
    provider = FixtureProvider.from_data(provenance.apply(raw, receipt, witness_key), policy)
    state = tmp_path / "state"
    store = Store(state)
    packet, _, _ = prepare(provider, policy, store)
    response = tmp_path / "response.json"
    response.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": packet["run_id"],
                "packet_digest": packet["packet_digest"],
                "session": {"tool": "test", "id": "session", "model": "model"},
                "review": {"findings": [], "notes": []},
            }
        )
    )
    report, _ = import_response(provider, policy, store, packet["run_id"], response)
    request = tmp_path / "request.json"
    request.write_text(json.dumps(release.plan(report, 12, "main")))

    api.pr["merged"] = True
    tip = "5" * 40
    api.branch.update(protected=True, commit={"sha": tip})
    writes = []

    class Transport:
        def request(self, method, route, params=None, body=None):
            if method == "POST" and route.endswith("/workflows/12/dispatches"):
                writes.append(body)
                return {}, False
            if route.endswith("/pulls/9"):
                return api.pr, False
            if route.endswith("/branches/main"):
                return api.branch, False
            if route.endswith(f"/compare/{HEAD}...{tip}"):
                return {"status": "ahead", "merge_base_commit": {"sha": HEAD}}, False
            raise AssertionError(route)

    monkeypatch.setattr("cirra.cli.GitHubClient", lambda *a, **kw: api)
    monkeypatch.setattr("cirra.extensions.JSONClient", lambda *a, **kw: Transport())
    monkeypatch.setattr("cirra.extensions.github_app_token", lambda *a, **kw: "test-token")
    monkeypatch.setenv("TEST_RELEASE_KEY", "test-key")
    monkeypatch.setenv("TEST_WITNESS_KEY", witness_key)
    args = [
        "dispatch-release",
        "--request",
        str(request),
        "--config",
        policy.path,
        "--state-dir",
        str(state),
        "--app-id",
        "1",
        "--installation-id",
        "2",
        "--app-key-env",
        "TEST_RELEASE_KEY",
        "--provenance",
        str(receipt),
        "--witness-key-env",
        "TEST_WITNESS_KEY",
    ]
    api.checks[0]["conclusion"] = "failure"
    assert main(args) == 2 and "no longer verified" in capsys.readouterr().err
    assert writes == []
    api.checks[0]["conclusion"] = "success"
    assert main(args) == 0
    assert len(writes) == 1 and writes[0]["inputs"]["cirra_expected_sha"] == HEAD


def test_redirect_does_not_forward_credentials():
    transport = Transport([(302, b"canary-secret", {"Location": "https://other.test/leak"})])
    with pytest.raises(GitHubError, match="redirect") as error:
        client_with(transport, token="canary-secret").get("pulls/9")
    assert "canary-secret" not in str(error.value)
    assert len(transport.requests) == 1
    assert isinstance(error.value, ProviderIdentityMismatch)


def test_worker_stops_strict_ci_collection_on_a_canonical_redirect():
    transport = Transport([(301, b"", {"Location": "https://api.github.com/repos/example/new"})])
    with pytest.raises(ProviderIdentityMismatch):
        checks_from(client_with(transport), HEAD, 2, strict_ci_reads=True)
    assert len(transport.requests) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 429])
def test_http_failure_is_safe_and_not_retried(status):
    transport = Transport([(status, b"canary-secret", {})])
    with pytest.raises(GitHubError) as error:
        client_with(transport, token="canary-secret").get("pulls/9")
    assert str(status) in str(error.value) and "canary-secret" not in str(error.value)
    assert len(transport.requests) == 1


def test_transient_read_failure_retries_but_is_bounded(monkeypatch):
    monkeypatch.setattr("cirra.github.time.sleep", lambda _: None)
    transport = Transport([(503, b"unavailable", {}), (200, b"{}", {})])
    assert client_with(transport).get("pulls/9") == ({}, False)
    assert len(transport.requests) == 2
    transport = Transport([URLError("canary-secret")] * 3)
    with pytest.raises(GitHubError, match="network") as error:
        client_with(transport).get("pulls/9")
    assert "canary-secret" not in str(error.value) and len(transport.requests) == 3
    assert "canary-secret" not in "".join(traceback.format_exception(error.value))
    assert isinstance(error.value, ReadUnavailable)
    transport = Transport([(503, b"", {})] * 3)
    with pytest.raises(ReadUnavailable, match="503"):
        client_with(transport).get("pulls/9")
    assert len(transport.requests) == 3


def test_size_and_deadline_limits():
    transport = Transport([(200, b"x" * 33, {})])
    with pytest.raises(GitHubError, match="byte limit"):
        client_with(transport, max_bytes=32).get("pulls/9")
    transport = Transport([])
    client = client_with(transport)
    client.deadline = 0
    with pytest.raises(ReadUnavailable, match="deadline"):
        client.get("pulls/9")
    assert transport.requests == []


@pytest.mark.parametrize(
    "repository", ["../repo", "example/repo?x=1", "example/repo/extra", "https://evil.test/repo"]
)
def test_reject_untrusted_repository_paths(repository):
    with pytest.raises(CirraError):
        GitHubClient(repository)


def test_collect_cli_and_snapshot_alias(github_policy, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("cirra.cli.GitHubClient", lambda *args, **kwargs: API())
    args = ["--config", github_policy.path, "--state-dir", str(tmp_path / "state")]
    assert (
        main(["collect", "--provider", "github", "--repo", "example/repo", "--change", "9", *args])
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["files"] == 1 and receipt["checks"] == 2
    assert main(["prepare-review", "--snapshot", receipt["snapshot_path"], *args]) == 0
    assert json.loads(capsys.readouterr().out)["review_outcome"] == "awaiting_session"
