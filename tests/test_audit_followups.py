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
