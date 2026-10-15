import copy
from dataclasses import replace

import pytest

from cirra import forgejo, gitlab
from cirra.domain import CirraError
from cirra.fixture import FixtureProvider
from cirra.policy import assess_checks

HEAD, BASE, TARGET = "2" * 40, "1" * 40, "3" * 40
TIME = "2026-10-15T00:00:00Z"


class GitLabAPI:
    def __init__(self):
        self.calls = []
        self.target, self.overflow, self.job_error = TARGET, False, False

    def get(self, route="", params=None):
        self.calls.append(route)
        if not route:
            return {"id": 123, "path_with_namespace": "group/repo"}, False
        if route == "merge_requests/9":
            return {
                "project_id": 123,
                "iid": 9,
                "sha": HEAD,
                "target_branch": "main",
                "diff_refs": {"head_sha": HEAD, "base_sha": BASE, "start_sha": TARGET},
            }, False
        if route == "repository/branches/main":
            return {"commit": {"id": self.target}}, False
        version = {
            "id": 1,
            "head_commit_sha": HEAD,
            "base_commit_sha": BASE,
            "start_commit_sha": TARGET,
            "state": "collected",
            "real_size": "2" if self.overflow else "1",
        }
        if route == "merge_requests/9/versions":
            return [version], False
        if route == "merge_requests/9/versions/1":
            return {
                **version,
                "diffs": [
                    {
                        "new_path": "add.py",
                        "old_path": "add.py",
                        "diff": "@@ -1 +1 @@\n-return a + b\n+return a - b\n",
                    }
                ],
            }, False
        if route == "merge_requests/9/pipelines":
            return [{"id": 5}, {"id": 6}], False
        if route in ("pipelines/5", "pipelines/6"):
            return {
                "id": int(route[-1]),
                "project_id": 123,
                "sha": HEAD,
                "source": "merge_request_event",
            }, False
        if route.endswith("/jobs"):
            if self.job_error:
                raise CirraError("pipeline jobs inaccessible")
            later = route == "pipelines/6/jobs"
            return [
                {
                    "id": 20 if later else 10,
                    "status": "failed" if later else "success",
                    "name": "test",
                    "commit": {"id": HEAD},
                    "web_url": "https://gitlab.com/group/repo/-/jobs/20",
                    "created_at": "2026-10-15T01:00:00Z" if later else TIME,
                }
            ], False
        raise AssertionError(route)


def gitlab_policy(inputs):
    _, config = inputs()
    return replace(
        config,
        reviewer_kind="session",
        reviewer_endpoint="session://manual",
        repositories=(
            {"provider": "gitlab", "instance": "https://gitlab.com", "repository_id": "123"},
        ),
        expected_checks=({"kind": "pipeline_job", "source_id": "123", "name": "test"},),
    )


def test_gitlab_versions_and_job_reruns_keep_identity_without_claiming_checkout(inputs):
    config, api = gitlab_policy(inputs), GitLabAPI()
    data = gitlab.collect("group/repo", 9, config, client=api)
    provider = FixtureProvider.from_data(data, config)
    assert provider.snapshot.diff_base_sha == BASE
    assert provider.snapshot.target_sha == TARGET
    assert not provider.snapshot.coverage_gaps
    assert [check["run_id"] for check in provider.checks] == ["5", "6"]
    ci = assess_checks(provider.checks, provider.snapshot, config)
    assert ci["expected"][0]["state"] == "failure"
    assert not ci["expected"][0]["verified_revision"]
    assert ci["expected"][0]["evidence"]["raw_state"] == "failed"


@pytest.mark.parametrize("condition", ["target", "overflow", "jobs"])
def test_gitlab_partial_inputs_remain_incomplete(inputs, condition):
    config, api = gitlab_policy(inputs), GitLabAPI()
    if condition == "target":
        api.target = "a" * 40
    elif condition == "overflow":
        api.overflow = True
    else:
        api.job_error = True
    provider = FixtureProvider.from_data(
        gitlab.collect("group/repo", 9, config, client=api), config
    )
    assert provider.snapshot.coverage_gaps or provider.check_errors


def test_gitlab_allowlist_precedes_change_reads(inputs):
    config, api = gitlab_policy(inputs), GitLabAPI()
    config = replace(config, repositories=())
    with pytest.raises(CirraError, match="allowlisted"):
        gitlab.collect("group/repo", 9, config, client=api)
    assert api.calls == [""]


class ForgejoAPI:
    instance = "https://forge.example"

    def get(self, route="", params=None):
        if not route:
            return {"id": 42, "full_name": "group/repo"}, False
        if route == "pulls/9":
            return {
                "number": 9,
                "head": {"sha": HEAD},
                "base": {"sha": TARGET, "repo": {"id": 42}},
                "merge_base": BASE,
                "changed_files": 1,
            }, False
        if route == "pulls/9.diff":
            return (
                "diff --git a/old.py b/new.py\nsimilarity index 80%\n--- a/old.py\n+++ b/new.py\n"
                "@@ -1 +1 @@\n-return a + b\n+return a - b\n",
                False,
            )
        if route == "pulls/9/files":
            return [
                {
                    "filename": "new.py",
                    "previous_filename": "old.py",
                    "status": "renamed",
                    "additions": 1,
                    "deletions": 1,
                }
            ], False
        if route.endswith("/statuses"):
            return [
                {
                    "id": 7,
                    "status": "success",
                    "creator": {"id": 8},
                    "context": "test",
                    "created_at": TIME,
                    "target_url": "https://forge.example/group/repo/actions/1",
                }
            ], False
        raise AssertionError(route)


def test_forgejo_native_diff_rename_and_status_contract(inputs):
    _, config = inputs()
    config = replace(
        config,
        reviewer_kind="session",
        repositories=(
            {"provider": "forgejo", "instance": "https://forge.example", "repository_id": "42"},
        ),
    )
    provider = FixtureProvider.from_data(
        forgejo.collect("group/repo", 9, config, client=ForgejoAPI()), config
    )
    assert provider.snapshot.files[0].old_path == "old.py"
    assert not provider.snapshot.coverage_gaps
    assert provider.checks[0]["source_id"] == "8"
    assert not provider.checks[0]["provenance"]["trusted"]


def test_gitlab_note_renderer_cannot_execute_quick_actions(inputs, fixture_data, tmp_path):
    from cirra.coordinator import review
    from cirra.publish import GitLabPublisher
    from cirra.store import Store

    fixture_data["reviewer_response"]["findings"][0]["evidence"] = "/merge\n/approve @maintainer"
    provider, config = inputs(data=fixture_data)
    report, _ = review(provider, config, Store(tmp_path / "state"))

    class API:
        def note(self, change, body, identifier):
            self.body = body
            return {"id": 5, "author": {"id": 42}, "system": False, "body": body}

    api = API()
    GitLabPublisher(api, "42").write(report, {"slot": "test"})
    assert "/merge" not in api.body and "/approve" not in api.body and "@maintainer" not in api.body


def test_github_publisher_ownership_and_neutral_conclusion(inputs, fixture_data, tmp_path):
    from cirra.coordinator import review
    from cirra.publish import GitHubPublisher
    from cirra.store import Store

    provider, config = inputs(data=fixture_data)
    report, _ = review(provider, config, Store(tmp_path / "state"))

    class HTTP:
        def request(self, method, route, params=None, body=None):
            self.body = copy.deepcopy(body)
            return {**body, "id": 5, "app": {"id": 42}}, False

    http = HTTP()
    result = GitHubPublisher("group/repo", "42", "test", http=http).write(report, {"slot": "test"})
    assert http.body["conclusion"] == "neutral"
    assert http.body["head_sha"] == provider.snapshot.head_sha
    assert result["metadata"] == {"slot": "test"}
