"""Authenticate saved webhook deliveries; payload revisions are only refresh hints."""

import hashlib
import hmac
import json
from pathlib import Path

from cirra.domain import CirraError, digest, number, string, utc_now
from cirra.publish import write_receipt
from cirra.reviewer import unique_object


def event_hint(provider, instance, raw, headers, secret, config):
    if len(raw) > 1024 * 1024 or not isinstance(headers, dict):
        raise CirraError("webhook body or headers exceed the accepted contract")
    normalized = {key.lower(): value for key, value in headers.items()}
    if len(normalized) != len(headers):
        raise CirraError("duplicate webhook header")
    if not isinstance(secret, str) or len(secret) < 16:
        raise CirraError("webhook secret must contain at least 16 characters")
    if provider == "gitlab":
        supplied = normalized.get("x-gitlab-token", "")
        expected = secret
        delivery = normalized.get("x-gitlab-event-uuid")
        kind = normalized.get("x-gitlab-event")
        supported = kind == "Merge Request Hook"
    else:
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if provider == "github":
            expected = "sha256=" + expected
            supplied = normalized.get("x-hub-signature-256", "")
            delivery, kind = normalized.get("x-github-delivery"), normalized.get("x-github-event")
        elif provider == "forgejo":
            supplied = normalized.get(
                "x-forgejo-signature", normalized.get("x-gitea-signature", "")
            )
            delivery = normalized.get("x-forgejo-delivery", normalized.get("x-gitea-delivery"))
            kind = normalized.get("x-forgejo-event", normalized.get("x-gitea-event"))
        else:
            raise CirraError("unsupported webhook provider")
        supported = kind == "pull_request"
    if not isinstance(supplied, str) or not hmac.compare_digest(expected, supplied):
        raise CirraError("webhook authentication failed")
    if not supported:
        raise CirraError(
            "unsupported webhook event; only pull/merge request refresh hints are accepted"
        )
    string(delivery, "webhook delivery identity", 200)
    try:
        payload = json.loads(raw, object_pairs_hook=unique_object)
        repo = payload["project"] if provider == "gitlab" else payload["repository"]
        repository_id = str(number(repo["id"], "repository ID", 1, 2**63 - 1, integral=True))
        name = repo["path_with_namespace"] if provider == "gitlab" else repo["full_name"]
        change = (
            payload["object_attributes"]["iid"]
            if provider == "gitlab"
            else payload["pull_request"]["number"]
        )
        number(change, "change number", 1, 2**31 - 1, integral=True)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise CirraError("webhook does not contain a usable repository/change identity") from None
    identity = {"provider": provider, "instance": instance, "repository_id": repository_id}
    if identity not in config.repositories:
        raise CirraError("webhook repository is not allowlisted")
    return {
        **identity,
        "repository": name,
        "change": change,
        "delivery": delivery,
        "payload_digest": hashlib.sha256(raw).hexdigest(),
    }


def ingest(store, hint, capture):
    key = digest([hint["provider"], hint["instance"], hint["delivery"]])
    path = store.path.parent / "events" / f"{key}.json"
    with store.lease("event:" + key, 30, 10) as claim:
        previous = json.loads(path.read_text()) if path.exists() else None
        if previous and previous["hint"] != hint:
            raise CirraError("webhook delivery identity was reused with another payload")
        if previous and previous["state"] == "captured":
            return {**previous, "duplicate": True}
        with store.transaction() as connection:
            store._fence(connection, claim)
            write_receipt(path, {"state": "accepted", "hint": hint, "accepted_at": utc_now()})
        snapshot_path = (
            capture()
        )  # Always fetch current provider state, never the payload diff/SHA.
        receipt = {
            "state": "captured",
            "hint": hint,
            "snapshot_path": str(Path(snapshot_path).resolve()),
            "observed_at": utc_now(),
            "duplicate": False,
        }
        with store.transaction() as connection:
            store._fence(connection, claim)
            write_receipt(path, receipt)
        return receipt
