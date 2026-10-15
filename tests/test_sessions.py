import copy
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cirra.coordinator import review
from cirra.domain import CirraError
from cirra.report import render_markdown
from cirra.session import import_response, prepare
from cirra.store import Store


def session_inputs(inputs, policy_data, fixture_data):
    policy_data["reviewer"] = {
        "kind": "session",
        "endpoint": "session://manual",
        "model": "operator-selected",
    }
    fixture_data.pop("reviewer_response", None)
    return inputs(data=fixture_data, policy=policy_data)


def reply(packet, findings=None):
    return {
        "schema_version": 1,
        "run_id": packet["run_id"],
        "packet_digest": packet["packet_digest"],
        "session": {"tool": "codex", "id": "session-123", "model": "reported-model"},
        "review": {"findings": findings or [], "notes": ["Reviewed only the supplied patch."]},
    }


def submit(tmp_path, provider, config, store, packet, response=None):
    path = tmp_path / "response.json"
    path.write_text(json.dumps(response or reply(packet)), encoding="utf-8")
    return import_response(provider, config, store, packet["run_id"], path)


def test_session_round_trip_without_fake_response(inputs, policy_data, fixture_data, tmp_path):
    provider, config = session_inputs(inputs, policy_data, fixture_data)
    store = Store(tmp_path / "state")
    packet, pending, reused = prepare(provider, config, store)
    assert reused is False
    assert pending["status"] == "incomplete"
    assert pending["review"]["outcome"] == "awaiting_session"
    assert packet["input"] == provider.packet()
    assert "reviewer_response" not in json.dumps(packet)
    again, _, reused = prepare(provider, config, store)
    assert reused is True and again == packet
    report, reused = submit(tmp_path, provider, config, store, packet)
    assert reused is False
    assert report["status"] == "complete"
    assert report["review"]["session"] == {
        **reply(packet)["session"],
        "provenance": "operator_supplied",
    }
    assert report["review"]["duration_seconds"] is None
    assert report["review"]["usage"] is None and report["review"]["cost"] is None
    assert "operator-supplied" in render_markdown(report)
    assert "offline reviewer" not in render_markdown(report)
    repeated, reused = submit(tmp_path, provider, config, store, packet)
    assert reused is True
    assert repeated["run_id"] == report["run_id"]
    _, completed, _ = prepare(provider, config, store)
    assert completed["review"]["outcome"] == "succeeded"


@pytest.mark.parametrize("field", ["run_id", "packet_digest"])
def test_response_must_match_packet(field, inputs, policy_data, fixture_data, tmp_path):
    provider, config = session_inputs(inputs, policy_data, fixture_data)
    store = Store(tmp_path / "state")
    packet, _, _ = prepare(provider, config, store)
    response = reply(packet)
    response[field] = "incorrect"
    with pytest.raises(CirraError, match="match"):
        submit(tmp_path, provider, config, store, packet, response)
    assert prepare(provider, config, store)[1]["review"]["outcome"] == "awaiting_session"


@pytest.mark.parametrize("change", ["head", "target", "base", "patch", "policy"])
def test_import_requires_original_revision_and_policy(
    change, inputs, policy_data, fixture_data, tmp_path
):
    provider, config = session_inputs(inputs, policy_data, fixture_data)
    store = Store(tmp_path / "state")
    packet, _, _ = prepare(provider, config, store)
    if change == "policy":
        policy_data["limits"]["max_findings"] += 1
    elif change == "patch":
        fixture_data["files"][0]["patch"] += "\n"
    else:
        key = {"head": "head_sha", "target": "target_sha", "base": "diff_base_sha"}[change]
        fixture_data["change"][key] = "f" * 40
    provider, config = inputs(data=fixture_data, policy=policy_data)
    with pytest.raises(CirraError, match="snapshot or policy"):
        submit(tmp_path, provider, config, store, packet)


def test_rereview_supersedes_old_response(inputs, policy_data, fixture_data, tmp_path):
    provider, config = session_inputs(inputs, policy_data, fixture_data)
    store = Store(tmp_path / "state")
    old, _, _ = prepare(provider, config, store)
    fresh, _, reused = prepare(provider, config, store, rereview=True)
    assert reused is False and fresh["run_id"] != old["run_id"]
    with pytest.raises(CirraError, match="superseded"):
        submit(tmp_path, provider, config, store, old)
    assert submit(tmp_path, provider, config, store, fresh)[0]["review"]["attempt"] == 2


def test_imported_response_is_immutable(inputs, policy_data, fixture_data, tmp_path):
    provider, config = session_inputs(inputs, policy_data, fixture_data)
    store = Store(tmp_path / "state")
    packet, _, _ = prepare(provider, config, store)
    submit(tmp_path, provider, config, store, packet)
    altered = reply(packet)
    altered["session"]["id"] = "different-session"
    with pytest.raises(CirraError, match="different response"):
        submit(tmp_path, provider, config, store, packet, altered)


def test_bad_findings_remain_incomplete(inputs, policy_data, fixture_data, tmp_path):
    finding = copy.deepcopy(fixture_data["reviewer_response"]["findings"][0])
    provider, config = session_inputs(inputs, policy_data, fixture_data)
    store = Store(tmp_path / "state")
    packet, _, _ = prepare(provider, config, store)
    finding["start_line"] = finding["end_line"] = 999
    report, _ = submit(tmp_path, provider, config, store, packet, reply(packet, [finding]))
    assert report["status"] == "incomplete"
    assert report["review"]["outcome"] == "malformed"
    assert "not a zero-finding review" in render_markdown(report)


@pytest.mark.parametrize("bad", ["duplicate", "oversize", "invalid", "session", "extra"])
def test_reject_invalid_envelope(bad, inputs, policy_data, fixture_data, tmp_path):
    provider, config = session_inputs(inputs, policy_data, fixture_data)
    store = Store(tmp_path / "state")
    packet, _, _ = prepare(provider, config, store)
    response = reply(packet)
    if bad == "session":
        response["session"]["id"] = ""
    if bad == "extra":
        response["command"] = "do not execute"
    raw = json.dumps(response)
    if bad == "duplicate":
        raw = raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    elif bad == "oversize":
        raw += " " * config.max_response_bytes
    elif bad == "invalid":
        raw = "not JSON"
    path = tmp_path / "bad.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(CirraError):
        import_response(provider, config, store, packet["run_id"], path)
    assert prepare(provider, config, store)[1]["status"] == "incomplete"


@pytest.mark.parametrize("stage", ["finish", "save_report"])
def test_import_resumes_durable_response(
    stage, inputs, policy_data, fixture_data, tmp_path, monkeypatch
):
    provider, config = session_inputs(inputs, policy_data, fixture_data)
    store = Store(tmp_path / "state")
    packet, _, _ = prepare(provider, config, store)
    with monkeypatch.context() as patch:

        def crash(*args):
            raise RuntimeError("process interrupted")

        patch.setattr(store, stage, crash)
        with pytest.raises(RuntimeError):
            submit(tmp_path, provider, config, store, packet)
    # Recovery works even if the imported file is no longer available.
    _, report, reused = prepare(provider, config, Store(tmp_path / "state"))
    assert reused is True
    assert report["status"] == "complete"
    assert report["review"]["session"]["id"] == "session-123"


def test_ci_refresh_preserves_session_result(inputs, policy_data, fixture_data, tmp_path):
    provider, config = session_inputs(inputs, policy_data, fixture_data)
    store = Store(tmp_path / "state")
    packet, _, _ = prepare(provider, config, store)
    submit(tmp_path, provider, config, store, packet)
    fixture_data["checks"][0]["raw_state"] = "failure"
    provider, config = inputs(data=fixture_data, policy=policy_data)
    same, report, reused = prepare(provider, config, store)
    assert same == packet and reused is True
    assert report["ci"]["outcome"] == "failed"
    assert report["review"]["outcome"] == "succeeded"


def test_concurrent_imports_accept_only_one_response(inputs, policy_data, fixture_data, tmp_path):
    provider, config = session_inputs(inputs, policy_data, fixture_data)
    store = Store(tmp_path / "state")
    packet, _, _ = prepare(provider, config, store)
    path = tmp_path / "shared.json"
    path.write_text(json.dumps(reply(packet)), encoding="utf-8")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: import_response(provider, config, store, packet["run_id"], path), range(2)
            )
        )
    assert sorted(reused for _, reused in results) == [False, True]
    assert all(report["status"] == "complete" for report, _ in results)


def test_wrong_command_cannot_use_fake_response(inputs, policy_data, fixture_data, tmp_path):
    provider, config = session_inputs(inputs, policy_data, fixture_data)
    with pytest.raises(CirraError, match="prepare-review"):
        review(provider, config, Store(tmp_path / "state"))


def test_session_cli_round_trip(inputs, policy_data, fixture_data, tmp_path):
    session_inputs(inputs, policy_data, fixture_data)
    common = [
        "--fixture",
        str(tmp_path / "change.json"),
        "--config",
        str(tmp_path / "policy.yaml"),
        "--state-dir",
        str(tmp_path / "state"),
    ]

    def invoke(command, *extra):
        return subprocess.run(
            [sys.executable, "-m", "cirra", command, *common, *extra],
            capture_output=True,
            text=True,
            timeout=30,
        )

    prepared = invoke("prepare-review")
    assert prepared.returncode == 0, prepared.stderr
    receipt = json.loads(prepared.stdout)
    assert receipt["review_outcome"] == "awaiting_session"
    packet = json.loads(Path(receipt["packet_path"]).read_text(encoding="utf-8"))
    response_path = tmp_path / "response.json"
    response_path.write_text(json.dumps(reply(packet)), encoding="utf-8")
    imported = invoke("import-review", "--run", packet["run_id"], "--response", str(response_path))
    assert imported.returncode == 0, imported.stderr
    report = json.loads(Path(json.loads(imported.stdout)["json_path"]).read_text(encoding="utf-8"))
    assert report["review"]["session"]["tool"] == "codex"
