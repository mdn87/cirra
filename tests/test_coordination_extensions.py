import copy
import hashlib
import hmac
import json
import sys
from dataclasses import replace

import pytest

from cirra import events, provenance, release
from cirra.coordinator import review
from cirra.dispatch import ClaudeReviewer, bounded_process, tool_environment
from cirra.domain import CirraError, canonical
from cirra.publish import publish, verify_gitlab_permission_receipt
from cirra.store import Store
from cirra.transport import RemoteUncertain


def test_reviewer_environment_excludes_credentials_and_runtime_injection():
    source = {
        "PATH": "safe",
        "HOME": "login-location",
        "GITLAB_TOKEN": "secret",
        "CIRRA_PUBLISHER_CANARY": "secret",
        "ANTHROPIC_API_KEY": "secret",
        "NODE_OPTIONS": "malicious",
        "HTTP_PROXY": "credentials",
        "CLAUDE_CONFIG_DIR": "evil",
    }
    env = tool_environment(source)
    assert env["HOME"] == "login-location"
    assert all(
        value != "secret" and value != "malicious" and value != "evil" for value in env.values()
    )


def test_cli_flags_disable_tools_plugins_hooks_and_shell_interpolation(
    inputs, tmp_path, monkeypatch
):
    _, config = inputs()
    config = replace(config, reviewer_kind="claude", reviewer_endpoint="cli://claude")
    calls = []

    def run(argv, prompt, directory, timeout, maximum):
        calls.append((argv, prompt))
        if argv[-1] == "--help":
            return (
                b"--tools --restricted --safe-mode --strict-mcp-config --settings "
                b"--no-session-persistence --max-budget-usd --json-schema"
            )
        return canonical(
            {
                "subtype": "success",
                "structured_output": {"findings": [], "notes": []},
                "session_id": "transport-id",
                "usage": {"output_tokens": 10},
            }
        ).encode()

    monkeypatch.setattr("cirra.dispatch.shutil.which", lambda name: sys.executable)
    monkeypatch.setattr("cirra.dispatch.bounded_process", run)
    reviewer = ClaudeReviewer(config, tmp_path / "scratch")
    assert json.loads(reviewer.review({"patch": "$(steal secrets); /merge"}))["findings"] == []
    argv, prompt = calls[-1]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "$(steal secrets)" in prompt and "$(steal secrets)" not in " ".join(argv)
    assert reviewer.metadata["session"]["id"] == "transport-id"


def test_process_timeout_and_output_limit(tmp_path):
    with pytest.raises(TimeoutError):
        bounded_process(
            [sys.executable, "-c", "import time; time.sleep(10)"], "", tmp_path, 0.1, 1000
        )
    with pytest.raises(CirraError, match="output limit"):
        bounded_process([sys.executable, "-c", "print('x'*10000)"], "", tmp_path, 5, 100)


def test_automatic_response_and_usage_survive_restart_and_ci_refresh(inputs, tmp_path):
    provider, config = inputs()
    config = replace(config, reviewer_kind="claude", reviewer_endpoint="cli://claude")

    class Reviewer:
        metadata = {"usage": {"output_tokens": 42}, "session": {"id": "transport"}}
        calls = 0

        def review(self, packet):
            self.calls += 1
            return '{"findings":[],"notes":[]}'

    reviewer = Reviewer()
    store = Store(tmp_path / "state")
    first, _ = review(provider, config, store, reviewer=reviewer)
    provider.checks[0]["raw_state"] = "failure"
    second, reused = review(provider, config, Store(tmp_path / "state"), reviewer=reviewer)
    assert reviewer.calls == 1 and reused and first["run_id"] == second["run_id"]
    assert second["review"]["usage"]["output_tokens"] == 42
    assert second["ci"]["outcome"] == "failed"


class Publisher:
    principal_id = "42"

    def __init__(self):
        self.objects, self.writes, self.lose_ack, self.hide = [], 0, False, False

    def find(self, report, slot):
        return (
            []
            if self.hide
            else copy.deepcopy(
                [item for item in self.objects if item["metadata"].get("slot") == slot]
            )
        )

    def write(self, report, metadata, identifier):
        self.writes += 1
        item = {"id": identifier or str(self.writes), "metadata": metadata}
        self.objects = [item]
        if self.lose_ack:
            raise RemoteUncertain("lost acknowledgement")
        return item


def captured(inputs, fixture_data):
    fixture_data["collection"] = {
        "source": "github_rest",
        "repository": "example/repo",
        "observed_at": "2026-10-15T00:00:00Z",
        "check_errors": [],
    }
    return inputs(data=fixture_data)


def test_publish_reuses_one_object_refreshes_ci_and_fences_older_config(
    inputs, fixture_data, tmp_path
):
    provider, config = captured(inputs, fixture_data)
    store, writer = Store(tmp_path / "state"), Publisher()
    first, _ = review(provider, config, store)
    one = publish(store, config, first["run_id"], lambda: provider, writer)
    provider.checks[0]["raw_state"] = "failure"
    two = publish(store, config, first["run_id"], lambda: provider, writer)
    assert one["native_id"] == two["native_id"]
    assert two["metadata"]["generation"] == 2
    assert store.latest_report(first["run_id"])["ci"]["outcome"] == "failed"
    other_config = replace(config, digest="new-config")
    newer, _ = review(provider, other_config, store)
    publish(store, other_config, newer["run_id"], lambda: provider, writer)
    with pytest.raises(CirraError, match="supersedes"):
        publish(store, config, first["run_id"], lambda: provider, writer)


def test_lost_ack_never_creates_blind_duplicate_and_reconcile_adopts(
    inputs, fixture_data, tmp_path
):
    provider, config = captured(inputs, fixture_data)
    store, writer = Store(tmp_path / "state"), Publisher()
    report, _ = review(provider, config, store)
    writer.lose_ack = True
    receipt = publish(store, config, report["run_id"], lambda: provider, writer)
    assert receipt["state"] == "uncertain"
    writer.hide = True
    receipt = publish(store, config, report["run_id"], lambda: provider, writer)
    assert writer.writes == 1 and receipt["state"] == "uncertain"
    writer.hide = False
    receipt = publish(store, config, report["run_id"], lambda: provider, writer, reconcile=True)
    assert receipt["state"] == "published" and writer.writes == 1


def test_publish_refuses_changed_base_and_duplicate_remote_objects(inputs, fixture_data, tmp_path):
    provider, config = captured(inputs, fixture_data)
    store, writer = Store(tmp_path / "state"), Publisher()
    report, _ = review(provider, config, store)
    provider.snapshot = replace(provider.snapshot, target_sha="a" * 40)
    with pytest.raises(CirraError, match="obsolete"):
        publish(store, config, report["run_id"], lambda: provider, writer)
    assert writer.writes == 0


def test_signed_checkout_witness_binds_state_attempt_and_repo(fixture_data, tmp_path):
    key = "independent-witness-key-32-bytes-or-longer"
    check = fixture_data["checks"][0]
    check["provenance"] = {"trusted": False, "tested_sha": None, "parents": []}
    statement = {
        "version": 1,
        "witness": "isolated-runner-observer",
        "observed_at": "2026-10-15T00:00:00Z",
        "checks": [
            {
                "binding": provenance.binding(fixture_data["change"], check),
                "tested_sha": "a" * 40,
                "parents": ["1" * 40, "2" * 40],
                "evidence": "Independent observer log 123",
            }
        ],
    }
    receipt = tmp_path / "witness.json"
    receipt.write_text(json.dumps(provenance.sign(statement, key)))
    assert provenance.apply(fixture_data, receipt, key)["checks"][0]["provenance"]["trusted"]
    for field, value in (("raw_state", "failure"), ("attempt", 2), ("native_id", "new-job")):
        changed = copy.deepcopy(fixture_data)
        changed["checks"][0][field] = value
        assert not provenance.apply(changed, receipt, key)["checks"][0]["provenance"]["trusted"]
    with pytest.raises(CirraError, match="signature"):
        provenance.apply(fixture_data, receipt, "wrong-key-but-long-enough-for-verification")


def test_webhook_authentication_delivery_dedup_and_current_state_refresh(inputs, tmp_path):
    _, config = inputs()
    config = replace(
        config,
        repositories=(
            {"provider": "github", "instance": "https://github.com", "repository_id": "42"},
        ),
    )
    secret = "an-independent-webhook-secret"
    raw = canonical(
        {
            "repository": {"id": 42, "full_name": "example/repo"},
            "pull_request": {"number": 1, "head": {"sha": "stale-untrusted-sha"}},
        }
    ).encode()
    headers = {
        "X-GitHub-Delivery": "delivery-1",
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": "sha256="
        + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
    }
    hint = events.event_hint("github", "https://github.com", raw, headers, secret, config)
    assert "stale-untrusted-sha" not in canonical(hint)
    calls = []

    def capture():
        calls.append(True)
        return tmp_path / "current.json"

    store = Store(tmp_path / "state")
    events.ingest(store, hint, capture)
    assert events.ingest(store, hint, capture)["duplicate"] and len(calls) == 1
    with pytest.raises(CirraError, match="authentication"):
        events.event_hint("github", "https://github.com", raw + b" ", headers, secret, config)


def test_release_refuses_incomplete_or_findings_and_lost_ack_requires_reconcile(
    inputs, fixture_data, tmp_path
):
    fixture_data["reviewer_response"] = {"findings": [], "notes": []}
    provider, config = captured(inputs, fixture_data)
    store = Store(tmp_path / "state")
    provider.checks[0]["provenance"]["definition_digest"] = "d" * 64
    config = replace(
        config,
        expected_checks=tuple(
            {**item, "definition_digest": "d" * 64} for item in config.expected_checks
        ),
    )
    report, _ = review(provider, config, store)
    request = release.plan(report, 12, "release-test")

    class HTTP:
        writes = 0

        def request(self, method, route, params=None, body=None):
            if method == "POST":
                self.writes += 1
                raise RemoteUncertain("lost acknowledgement")
            if "/pulls/" in route:
                return {
                    "merged": True,
                    "head": {"sha": provider.snapshot.head_sha},
                    "base": {
                        "ref": "release-test",
                        "repo": {"id": provider.snapshot.repository_id},
                    },
                }, False
            if "/branches/" in route:
                return {"protected": True, "commit": {"sha": provider.snapshot.head_sha}}, False
            if "/compare/" in route:
                return {
                    "status": "identical",
                    "merge_base_commit": {"sha": provider.snapshot.head_sha},
                }, False
            return {"workflow_runs": []}, False

    http = HTTP()
    one = release.dispatch(request, store, config, lambda: provider, http)
    two = release.dispatch(request, store, config, lambda: provider, http)
    assert one["state"] == two["state"] == "uncertain" and http.writes == 1
    with pytest.raises(CirraError, match="verified CI"):
        release.plan({**report, "status": "incomplete"}, 12, "release-test")


def test_publisher_scope_receipt_cannot_be_reused_for_another_token(tmp_path):
    token = "publisher-test-token"
    receipt = {
        "version": 1,
        "instance": "https://gitlab.com",
        "repository_id": "42",
        "principal_id": "7",
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "checked_at": "2026-10-15T00:00:00Z",
        "evidence": "Operator inspected selected project and permissions in token settings",
        "note_read_create_update": True,
        "no_merge_or_repository_write": True,
    }
    path = tmp_path / "permissions.json"
    path.write_text(json.dumps(receipt))
    assert verify_gitlab_permission_receipt(path, token, "42", "7") == receipt
    with pytest.raises(CirraError, match="scope evidence"):
        verify_gitlab_permission_receipt(path, "another-token", "42", "7")
