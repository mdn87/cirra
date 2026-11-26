"""Recover signed GitHub deliveries from the provider into a local Cirra queue."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from cirra import events
from cirra.domain import CirraError, digest, number, string
from cirra.github import API_VERSION, native_id, record, repository_name
from cirra.job_queue import Queue
from cirra.reviewer import unique_object
from cirra.transport import JSONClient, ResourceMissing
from cirra.webhook import MAX_BODY, accept


def timestamp(value):
    try:
        when = datetime.fromisoformat(string(value, "delivery timestamp", 64))
        if when.tzinfo is not None:
            return when.astimezone(UTC)
    except (ValueError, OverflowError):
        pass
    raise CirraError("delivery timestamps require an ISO date and time zone")


def recent_ids(http, path, max_pages):
    """Read all retained PR deliveries. GitHub's clock can straddle the local snapshot."""
    params, seen, deliveries = {"per_page": 100}, set(), {}
    for _ in range(max_pages):
        rows, more = http.request("GET", path, params)
        if not isinstance(rows, list) or len(rows) > 100:
            raise CirraError("invalid GitHub delivery list")
        for row in rows:
            if record(row).get("event") == "pull_request":
                when = timestamp(row.get("delivered_at"))
                identifier = int(native_id(row.get("id")))
                deliveries[identifier] = when
        if not more:
            return sorted(deliveries, key=lambda identifier: (deliveries[identifier], identifier))
        # Extract only the cursor. The next request stays on our fixed repository and hook path.
        links = re.findall(r'<([^<>]+)>\s*;\s*rel="next"', http.last_link)
        cursors = parse_qs(urlsplit(links[0]).query).get("cursor", []) if len(links) == 1 else []
        if len(cursors) != 1 or len(cursors[0]) > 1024 or cursors[0] in seen:
            raise CirraError("missing or repeated GitHub delivery cursor")
        seen.add(cursors[0])
        params = {**params, "cursor": cursors[0]}
    raise CirraError("GitHub delivery page limit reached before completing the window")


def signed_body(route, secret, headers, payload):
    # GitHub can return an object instead of its original bytes. Only a byte-for-byte
    # match to the archived provider signature may enter the ordinary admission path.
    if isinstance(payload, str):
        candidates = (payload,)
    elif isinstance(payload, dict):
        candidates = (
            json.dumps(payload, ensure_ascii=ascii_only, separators=(",", ":"), allow_nan=False)
            for ascii_only in (False, True)
        )
    else:
        raise CirraError("archived GitHub request has no JSON payload")
    for candidate in candidates:
        raw = candidate.encode("utf-8")
        if len(raw) <= MAX_BODY:
            try:
                events.authenticate(route.provider, raw, headers, secret)
            except CirraError:
                continue
            return raw
    raise CirraError("could not reproduce the signed bytes; request GitHub redelivery")


def restore(detail, identifier, hook, route, secret, queue):
    request = record(record(detail).get("request"))
    original_headers = record(request.get("headers"))
    headers = {key.lower(): value for key, value in original_headers.items()}
    guid = string(detail.get("guid"), "archived delivery GUID", 200)
    target = urlsplit(string(detail.get("url"), "archived destination", 2048))
    if (
        route.provider != "github"
        or native_id(detail.get("id")) != str(identifier)
        or native_id(detail.get("repository_id")) != route.repository_id
        or target.scheme != "https"
        or not target.hostname
        or target.path != "/hooks/" + route.name
        or detail.get("event") != "pull_request"
        or headers.get("x-github-event") != "pull_request"
        or headers.get("x-github-delivery") != guid
        or headers.get("x-github-hook-id") != str(hook)
        or len(headers) != len(original_headers)
    ):
        raise CirraError("archived delivery does not match the requested hook, route or identity")
    raw = signed_body(route, secret, headers, request.get("payload"))
    was_accepted = False
    if detail.get("status_code") == 202:
        # A successful Cirra response is an independent receipt for the original body/route.
        reply = record(detail.get("response")).get("payload")
        if isinstance(reply, str):
            try:
                reply = json.loads(reply, object_pairs_hook=unique_object)
            except (ValueError, RecursionError):
                raise CirraError("invalid original Cirra response receipt") from None
        state = record(reply).get("state")
        if state == "accepted":
            was_accepted = True
            expected = digest([route.name, hashlib.sha256(raw).hexdigest()])
            if reply.get("job_id") != expected:
                raise CirraError("original admission receipt disagrees with the signed delivery")
        elif state == "ignored":
            # The original receiver deliberately discarded this event.
            return {"state": "ignored", "reason": "original response"}
        else:
            raise CirraError("unrecognized original Cirra response receipt")
    result = accept(route, raw, headers, secret, queue)
    if was_accepted and result["state"] != "accepted":
        raise CirraError("current route would ignore a previously accepted delivery")
    return result


def recover(config, args, credential):
    route = config.routes.get(args.route)
    if route is None or route.provider != "github":
        raise CirraError("webhook recovery requires a configured GitHub route")
    hook = native_id(args.hook_id)
    if args.snapshot_at is not None:
        snapshot = timestamp(args.snapshot_at)
        now = datetime.now(UTC)
        if not now - timedelta(days=3) < snapshot <= now:
            raise CirraError("the snapshot must be within GitHub's last three days of deliveries")
    secret, token = credential(route.secret_env), credential(args.token_env)
    if not isinstance(secret, str) or len(secret) < 32 or not token:
        raise CirraError("recovery requires the route secret and a GitHub webhook reader")
    path = f"/repos/{repository_name(route.repository)}/hooks/{hook}/deliveries"
    http = JSONClient(
        "https://api.github.com",
        {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": API_VERSION},
        timeout=600,
    )
    if args.delivery_id:
        ids = list(dict.fromkeys(int(native_id(value)) for value in args.delivery_id))
    else:
        ids = recent_ids(http, path, number(args.max_pages, "page limit", 1, 100, integral=True))
    from cirra.coordination import connect

    queue = connect(config, args.state_dir, credential) or Queue(args.state_dir, config.max_jobs)
    result = {"seen": len(ids), "accepted": 0, "duplicate": 0, "ignored": 0, "unresolved": []}
    for index, identifier in enumerate(ids):
        try:
            detail, _ = http.request("GET", f"{path}/{identifier}")
        except ResourceMissing:
            result["unresolved"].append(identifier)
            continue
        except CirraError as exc:
            result["unresolved"].extend(ids[index:])
            result["error"] = str(exc)
            break
        try:
            outcome = restore(detail, identifier, hook, route, secret, queue)
        except (CirraError, ValueError, RecursionError):
            result["unresolved"].append(identifier)
            continue
        except OSError:
            result["unresolved"].extend(ids[index:])
            result["error"] = "local queue storage unavailable"
            break
        key = "duplicate" if outcome.get("duplicate") else outcome["state"]
        result[key] += 1
    return result
