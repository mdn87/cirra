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
