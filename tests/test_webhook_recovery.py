import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
import test_services

from cirra.domain import CirraError
from cirra.job_queue import Queue
from cirra.webhook import accept
from cirra.webhook_recovery import recent_ids, restore

route = test_services.route
SECRET = test_services.SECRET

HOOK = 717
GUID = "abde7789-1654-48a7-ba09-f2be88497a2e"
ID = 3856186469120999424
NOW = datetime.now(UTC).replace(microsecond=0)


def archived(route, queue):
    raw, headers = test_services.delivery(identity=GUID, title="caf\u00e9")
    payload = json.loads(raw)
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    headers["X-Hub-Signature-256"] = (
        "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    )
    headers["X-GitHub-Hook-ID"] = str(HOOK)
    original = accept(route, raw, headers, SECRET, queue)
    return {
        "id": ID,
        "guid": GUID,
        "event": "pull_request",
        "repository_id": 42,
        "url": "https://old.example/hooks/" + route.name,
        "request": {"headers": headers, "payload": payload},
        "status_code": 202,
        "response": {"payload": json.dumps(original)},
    }, original


def test_lost_origin_recovers_same_job_and_replays_idempotently(route, tmp_path):
    upstream, expected = archived(route, Queue(tmp_path / "primary"))
    secondary = Queue(tmp_path / "standby")
    assert restore(upstream, ID, HOOK, route, SECRET, secondary) == expected
    assert restore(upstream, ID, HOOK, route, SECRET, secondary)["duplicate"]
    assert len(secondary.pending()) == 1
    text = json.dumps(secondary.pending())
    assert SECRET not in text and "caf" not in text


@pytest.mark.parametrize(
    "change", ("id", "guid", "event", "repository", "route", "hook", "body", "receipt")
)
def test_mismatches_leave_standby_empty(route, tmp_path, change):
    upstream, _ = archived(route, Queue(tmp_path / "primary"))
    if change == "id":
        upstream["id"] += 1
    elif change == "guid":
        upstream["guid"] += "a"
    elif change == "event":
        upstream["event"] = "ping"
    elif change == "repository":
        upstream["repository_id"] = 61
    elif change == "route":
        upstream["url"] = "https://old.example/hooks/a-different-route"
    elif change == "hook":
        upstream["request"]["headers"]["X-GitHub-Hook-ID"] = "788"
    elif change == "body":
        upstream["request"]["payload"]["action"] = "edited"
    else:
        upstream["response"]["payload"] = json.dumps({"state": "accepted", "job_id": "b" * 64})
    queue = Queue(tmp_path / "secondary")
    with pytest.raises(CirraError):
        restore(upstream, ID, HOOK, route, SECRET, queue)
    assert not queue.pending()


def test_delivery_missed_by_receiver_can_be_recovered(route, tmp_path):
    upstream, expected = archived(route, Queue(tmp_path / "primary"))
    upstream.update(status_code=503, response={"payload": "no origin"})
    assert restore(upstream, ID, HOOK, route, SECRET, Queue(tmp_path / "standby")) == expected


def test_cursor_pages_keep_exact_ids_and_report_an_incomplete_window():
    recent = {"event": "pull_request", "delivered_at": NOW.isoformat(), "id": ID}
    older = {**recent, "delivered_at": (NOW - timedelta(days=1)).isoformat(), "id": ID - 1}

    class API:
        def request(self, method, path, params):
            assert method == "GET" and path == "/repos/example/repo/hooks/717/deliveries"
            if "cursor" not in params:
                self.last_link = (
                    "<https://api.github.com/repositories/42/hooks/717/deliveries"
                    '?cursor=abc%2B1>; rel="next"'
                )
                return [recent], True
            assert params["cursor"] == "abc+1"
            return [older, {**recent, "id": ID - 2}], False

    api = API()
    since = NOW - timedelta(minutes=1)
    assert set(recent_ids(api, "/repos/example/repo/hooks/717/deliveries", since, 2)) == {
        ID,
        ID - 2,
    }
    with pytest.raises(CirraError, match="page limit"):
        recent_ids(api, "/repos/example/repo/hooks/717/deliveries", since, 1)


def test_cli_uses_read_access_and_exits_nonzero_for_an_unrecoverable_delivery(
    route, tmp_path, monkeypatch, capsys
):
    from cirra.cli import main

    upstream, _ = archived(route, Queue(tmp_path / "primary"))
    damaged = {**upstream, "id": ID + 1, "guid": "wrong-delivery"}
    config = test_services.settings(route)
    monkeypatch.setattr("cirra.service_cli.load_service", lambda _: config)
    monkeypatch.setenv("RECOVERY_READER", "fixture-reader")
    monkeypatch.setenv("HOOK_SECRET", SECRET)
    reads = []

    class API:
        def __init__(self, origin, headers, **options):
            assert (
                origin == "https://api.github.com"
                and headers["Authorization"] == "Bearer fixture-reader"
            )

        def request(self, method, path):
            reads.append((method, path))
            return (upstream if path.endswith(str(ID)) else damaged), False

    monkeypatch.setattr("cirra.webhook_recovery.JSONClient", API)
    args = [
        "recover-webhooks",
        "--service-config",
        "test.yaml",
        "--route",
        "test",
        "--state-dir",
        str(tmp_path / "standby"),
        "--hook-id",
        str(HOOK),
        "--token-env",
        "RECOVERY_READER",
        "--delivery-id",
        str(ID),
        "--delivery-id",
        str(ID + 1),
    ]
    assert main(args) == 1
    counts = json.loads(capsys.readouterr().out)
    assert counts["accepted"] == 1 and counts["unresolved"] == [ID + 1]
    assert reads == [
        ("GET", f"/repos/example/repo/hooks/{HOOK}/deliveries/{i}") for i in (ID, ID + 1)
    ]
