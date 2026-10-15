import copy
import io
import json
import traceback
from dataclasses import replace
from email.message import Message
from urllib.error import URLError
from urllib.request import HTTPSHandler, build_opener
from urllib.response import addinfourl

import pytest

from cirra.cli import main
from cirra.domain import CirraError, digest
from cirra.fixture import FixtureProvider
from cirra.github import GitHubClient, GitHubError, NoRedirect, collect, export_capture
from cirra.policy import assess_checks
from cirra.session import import_response, prepare
from cirra.store import Store

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
            "base": {"sha": TARGET, "repo": {"id": 123}},
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
    assert api.calls[2] == (f"compare/{TARGET}...{HEAD}", {"per_page": 1, "page": 1})
    assert api.calls[-1][0] == "pulls/9"
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


@pytest.mark.parametrize("side", ["head", "base"])
def test_revision_change_during_collection_retries_snapshot(side, github_policy):
    api = API()
    old = copy.deepcopy(api.pr)
    old[side]["sha"] = "a" * 40
    api.pr_sequence = [old, api.pr, api.pr, api.pr]
    if side == "base":
        old_compare = copy.deepcopy(api.comparison)
        old_compare["base_commit"]["sha"] = "a" * 40
        api.overrides[(f"compare/{'a' * 40}...{HEAD}", 1)] = (old_compare, False)
    data = collect("example/repo", 9, github_policy, client=api)
    assert data["change"]["head_sha"] == HEAD
    assert data["change"]["target_sha"] == TARGET
    assert len([route for route, _ in api.calls if route.startswith("compare/")]) == 2


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
    assert api.calls[3][1]["filter"] == "all"


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


def test_redirect_does_not_forward_credentials():
    transport = Transport([(302, b"canary-secret", {"Location": "https://other.test/leak"})])
    with pytest.raises(GitHubError, match="redirect") as error:
        client_with(transport, token="canary-secret").get("pulls/9")
    assert "canary-secret" not in str(error.value)
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


def test_size_and_deadline_limits():
    transport = Transport([(200, b"x" * 33, {})])
    with pytest.raises(GitHubError, match="byte limit"):
        client_with(transport, max_bytes=32).get("pulls/9")
    transport = Transport([])
    client = client_with(transport)
    client.deadline = 0
    with pytest.raises(GitHubError, match="deadline"):
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
