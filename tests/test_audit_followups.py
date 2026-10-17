"""Regressions for the 2026-10-17 production audit findings on cirra."""

import io
import time
from email.message import Message
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from cirra.github import GitHubClient, GitHubRateLimited
from cirra.job_queue import Queue
from cirra.worker import work_once


class LimitedOpener:
    def __init__(self, status, headers):
        self.status, self.headers = status, headers

    def open(self, request, timeout=None):
        headers = Message()
        for key, value in self.headers.items():
            headers[key] = value
        raise HTTPError(request.full_url, self.status, "limited", headers, io.BytesIO(b""))


@pytest.mark.parametrize(
    "status, headers, delay",
    [
        (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "+1800"}, 1800),
        (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "+999999"}, 3600),
        (429, {"Retry-After": "120"}, 120),
        (429, {}, 60),
    ],
)
def test_github_rate_limit_carries_the_provider_reset_time(status, headers, delay):
    # C001/C012: a rate limit is not an ordinary failure; it says when to retry.
    # Resets are capped at one hour, GitHub's quota window.
    now = time.time()
    headers = {
        key: str(int(now) + int(value[1:])) if value.startswith("+") else value
        for key, value in headers.items()
    }
    client = GitHubClient("owner/repo", opener=LimitedOpener(status, headers))
    with pytest.raises(GitHubRateLimited) as raised:
        client.get()
    assert now + delay - 2 <= raised.value.retry_at <= time.time() + delay + 1


def test_rate_limited_job_waits_for_reset_without_spending_an_attempt(tmp_path):
    route = SimpleNamespace(
        name="repo",
        digest="v1",
        provider="github",
        instance="https://github.com",
        repository="owner/repo",
        repository_id="42",
    )
    queue = Queue(tmp_path)
    queue.request(route, 7)
    config = SimpleNamespace(
        routes={"repo": route}, max_attempts=3, max_jobs_per_day=30, retry_seconds=30
    )
    reset = time.time() + 1800

    def limited(*_):
        raise GitHubRateLimited("GitHub rate limit reached (HTTP 403); retry later", reset)

    for _ in range(5):
        job = work_once(config, queue, limited)
        assert job["state"] == "retry"
        assert job["attempts"] == 0
        assert job["not_before"] >= reset
        job["not_before"] = 0  # simulate the reset passing
        queue.save(job)


def health_state(tmp_path, *, heartbeat_age=10, attention_age=None, reconcile=None, used=1):
    from datetime import UTC, datetime, timedelta

    from cirra.publish import write_receipt

    def stamp(age):
        return (datetime.now(UTC) - timedelta(seconds=age)).isoformat()

    queue = Queue(tmp_path)
    if heartbeat_age is not None:
        write_receipt(
            tmp_path / "worker-status.json",
            {"state": "running", "heartbeat_at": stamp(heartbeat_age)},
            history=False,
        )
    if attention_age is not None:
        route = SimpleNamespace(
            name="repo",
            digest="v1",
            provider="github",
            instance="https://github.com",
            repository="owner/repo",
            repository_id="42",
        )
        job_id = queue.request(route, 7)["job_id"]
        job = queue.read(queue.directory / (job_id + ".json"))
        job.update(state="attention", error="retry limit reached", updated_at=stamp(attention_age))
        queue.save(job)
    if reconcile is not None:
        write_receipt(tmp_path / "reconciliation.json", reconcile, history=False)
    write_receipt(queue.usage_path(), {"attempts": used, "maximum": 30}, history=False)
    return queue


def test_health_is_quiet_for_a_running_idle_worker(tmp_path):
    from cirra.health import check

    health_state(tmp_path, reconcile={"routes": [{"route": "repo", "jobs": []}]})
    assert check(tmp_path) == {"healthy": True, "problems": []}


@pytest.mark.parametrize(
    "state, expected",
    [
        ({"heartbeat_age": 5000}, "heartbeat"),
        ({"heartbeat_age": None}, "heartbeat"),
        ({"attention_age": 60}, "attention"),
        ({"reconcile": {"routes": [{"route": "repo", "error": "provider down"}]}}, "reconcile"),
        (
            {
                "reconcile": {
                    "routes": [{"route": "repo", "jobs": [], "branch_errors": {"main": "x"}}]
                }
            },
            "reconcile",
        ),
        ({"used": 30}, "daily"),
    ],
)
def test_health_reports_what_nothing_else_watches(tmp_path, state, expected):
    # Production audit C020/C021/C056-C058/C061/C062: stale heartbeats, jobs in
    # attention, reconcile errors and an exhausted budget were invisible.
    from cirra.health import check

    health_state(tmp_path, **state)
    result = check(tmp_path)
    assert result["healthy"] is False
    assert any(problem["kind"] == expected for problem in result["problems"]), result


def test_old_attention_jobs_do_not_alert_forever(tmp_path):
    from cirra.health import check

    health_state(tmp_path, attention_age=3 * 86400)
    assert check(tmp_path)["healthy"] is True


def test_unreadable_queue_file_is_reported_and_does_not_crash_the_worker(tmp_path):
    from cirra.health import check

    queue = health_state(tmp_path)
    (queue.directory / ("0" * 64 + ".json")).write_text("{not json", encoding="utf-8")
    assert queue.pending() == []
    result = check(tmp_path)
    assert any(problem["kind"] == "queue" for problem in result["problems"]), result


def test_store_upgrades_through_ordered_migrations_and_refuses_newer_schemas(tmp_path, monkeypatch):
    # Production audit C081: the first schema change would otherwise force a
    # fresh state directory and repeat model calls.
    import sqlite3

    from cirra import store
    from cirra.domain import CirraError

    store.Store(tmp_path)
    monkeypatch.setattr(store, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(
        store, "MIGRATIONS", {1: ["ALTER TABLE reports ADD COLUMN migrated_note TEXT"]}
    )
    store.Store(tmp_path)
    with sqlite3.connect(tmp_path / "cirra.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = [row[1] for row in connection.execute("PRAGMA table_info(reports)")]
    assert "migrated_note" in columns
    store.Store(tmp_path)  # already current: no-op
    monkeypatch.setattr(store, "SCHEMA_VERSION", 1)
    with pytest.raises(CirraError, match="newer"):
        store.Store(tmp_path)


def test_hashed_locks_cover_every_declared_dependency():
    # Production audit C108: the deployed venv must be reproducible from hashed locks.
    import re
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    def names(requirements):
        return {re.split(r"[<>=\[ ;]", item, maxsplit=1)[0].lower() for item in requirements}

    def locked(path):
        text = (root / "requirements" / path).read_text(encoding="utf-8")
        assert all("--hash=sha256:" in block for block in text.split("\n\n") if "==" in block)
        return {line.split("==")[0].lower() for line in text.splitlines() if "==" in line}

    service = names(project["dependencies"] + project["optional-dependencies"]["publish"])
    dev = service | names(project["optional-dependencies"]["dev"])
    assert service <= locked("service.txt")
    assert dev <= locked("dev.txt")


def test_observer_pins_detects_and_repins_a_stale_definition_digest(tmp_path, policy_data):
    # Production audit C006/C055/C104 follow-through: the 2026-10-17 rollout
    # deployed a new observer supervisor while policies still pinned the old
    # digest, so the worker refused CI until an operator re-pinned by hand.
    import shutil
    from pathlib import Path

    import yaml

    from cirra.observer import NAME, load_observer
    from cirra.pins import check_pins

    root = Path(__file__).resolve().parents[1]
    shutil.copy(root / "examples" / "observer.yaml", tmp_path / "observer.yaml")
    current = load_observer(tmp_path / "observer.yaml")["definition_digest"]
    stale = "f" * 64
    policy_data["reviewer"] = {"kind": "claude", "endpoint": "cli://claude", "model": "test"}
    policy_data["repositories"] = [
        {"provider": "github", "instance": "https://github.com", "repository_id": "42"}
    ]
    policy_data["ci"] = {
        "mode": "required",
        "expected": [
            {"kind": "check_run", "source_id": "5", "name": NAME, "definition_digest": stale}
        ],
    }
    (tmp_path / "policy.yaml").write_text(yaml.safe_dump(policy_data))
    service = {
        "version": 1,
        "routes": [
            {
                "name": "test",
                "provider": "github",
                "repository": "example/repo",
                "repository_id": "42",
                "policy": "policy.yaml",
                "webhook_secret_env": "HOOK_SECRET",
                "publisher": {"app_id": 5, "installation_id": 6, "private_key_file": "app.pem"},
                "observer": "observer.yaml",
                "witness_key_env": "WITNESS_KEY",
            }
        ],
    }
    (tmp_path / "service.yaml").write_text(yaml.safe_dump(service))

    [row] = check_pins(tmp_path / "service.yaml")
    assert row == {"route": "test", "observer_digest": current, "pinned": [stale], "ok": False}
    [row] = check_pins(tmp_path / "service.yaml", repin=True)
    assert row["ok"] and row["repinned"]
    assert stale in (tmp_path / "policy.yaml.before-repin").read_text()
    [row] = check_pins(tmp_path / "service.yaml")
    assert row["ok"] and row["pinned"] == [current]


def test_deploy_script_and_its_remote_half_are_valid_lf_bash():
    # Production audit C006/C055/C104: deploy and rollback were prose only.
    import shutil
    import subprocess
    from pathlib import Path

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")
    script = Path(__file__).resolve().parents[1] / "scripts" / "deploy-release.sh"
    assert b"\r" not in script.read_bytes()
    subprocess.run([bash, "-n", str(script)], check=True)
    remote = subprocess.run(
        [bash, str(script), "--print-remote"], check=True, capture_output=True
    ).stdout
    assert b"\r" not in remote and b"observer-pins" in remote and b"--require-hashes" in remote
    assert b"flock -n 9" in remote  # concurrent deploys refuse instead of racing
    subprocess.run([bash, "-n"], input=remote, check=True)
