"""Explicit advisory writes with durable intents, ownership checks and reconciliation."""

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from cirra.domain import CirraError, canonical, digest, fields, number, timestamp, utc_now
from cirra.github import API_VERSION, native_id, record, repository_name
from cirra.policy import assess_checks
from cirra.report import make_report, render_markdown
from cirra.transport import JSONClient, RemoteUncertain, pages

NAME = "cirra/review"


def verify_gitlab_permission_receipt(path, token, repository_id, principal_id):
    """Require the operator's token-setup evidence where introspection is unavailable.

    GitLab fine-grained tokens may intentionally lack User/Personal access token
    read. A UI-verified receipt is an explicit trust input, never inferred from a
    successful read or an unsafe probe of merge/write endpoints.
    """
    raw = Path(path).read_bytes()
    if len(raw) > 16384:
        raise CirraError("publisher permission receipt exceeds 16 KiB")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise CirraError("invalid publisher permission receipt") from None
    fields(
        value,
        [
            "version",
            "instance",
            "repository_id",
            "principal_id",
            "token_sha256",
            "checked_at",
            "evidence",
            "note_read_create_update",
            "no_merge_or_repository_write",
        ],
        label="permission receipt",
    )
    if (
        value["version"] != 1
        or value["instance"] != "https://gitlab.com"
        or value["repository_id"] != str(repository_id)
        or value["principal_id"] != str(principal_id)
        or value["token_sha256"] != hashlib.sha256(token.encode()).hexdigest()
        or value["note_read_create_update"] is not True
        or value["no_merge_or_repository_write"] is not True
        or not isinstance(value["evidence"], str)
        or not value["evidence"].strip()
    ):
        raise CirraError("publisher credential lacks matching operator-verified scope evidence")
    timestamp(value["checked_at"])
    return value


def marker(metadata):
    return "<!-- cirra:" + canonical(metadata) + " -->"


def metadata_from(body):
    prefix = "<!-- cirra:"
    if not isinstance(body, str) or not body.startswith(prefix):
        return None
    try:
        result = json.loads(body[len(prefix) : body.index(" -->")])
        return result if isinstance(result, dict) else None
    except (ValueError, RecursionError):
        return None


def github_app_token(app_id, installation_id, key, repository_id, *, capability="checks"):
    """Mint a separate token restricted server-side to this repo and Checks write."""
    try:
        import jwt
    except ImportError:
        raise CirraError(
            "GitHub App publication requires the optional cirra[publish] dependencies"
        ) from None
    for value in (app_id, installation_id, repository_id):
        number(int(value), "GitHub App identity", 1, 2**63 - 1, integral=True)
    now = int(time.time())
    try:
        signed = jwt.encode(
            {"iat": now - 60, "exp": now + 300, "iss": str(app_id)}, key, algorithm="RS256"
        )
    except Exception:
        raise CirraError("GitHub App signing key is invalid") from None
    app = JSONClient(
        "https://api.github.com",
        {"Authorization": f"Bearer {signed}", "X-GitHub-Api-Version": API_VERSION},
    )
    installation, _ = app.request("GET", f"/app/installations/{installation_id}")
    if str(record(installation).get("app_id")) != str(app_id):
        raise CirraError("installation belongs to another GitHub App")
    if capability not in ("checks", "actions"):
        raise CirraError("unsupported GitHub App capability")
    permissions = {capability: "write", "metadata": "read"}
    token, _ = app.request(
        "POST",
        f"/app/installations/{installation_id}/access_tokens",
        body={"repository_ids": [int(repository_id)], "permissions": permissions},
    )
    if record(token).get("permissions") != permissions:
        raise CirraError("GitHub did not grant the exact restricted publisher permissions")
    repositories = token.get("repositories", [])
    if not isinstance(repositories, list) or [
        str(record(item).get("id")) for item in repositories
    ] != [str(repository_id)]:
        raise CirraError("GitHub publisher token is not restricted to the selected repository")
    from cirra.domain import string

    return string(token.get("token"), "GitHub installation token")


class GitHubPublisher:
    def __init__(self, repository, principal_id, token, *, http=None):
        self.principal_id = str(principal_id)
        number(int(self.principal_id), "publisher App ID", 1, 2**63 - 1, integral=True)
        self.prefix = "/repos/" + repository_name(repository)
        self.http = http or JSONClient(
            "https://api.github.com",
            {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": API_VERSION},
        )

    def find(self, report, slot):
        head = report["snapshot"]["head_sha"]

        def get(route, params):
            data, more = self.http.request("GET", self.prefix + "/" + route, params)
            return record(data).get("check_runs"), more

        found = pages(
            get, f"commits/{head}/check-runs", params={"filter": "all", "check_name": NAME}
        )
        return [
            self.object(item)
            for item in found
            if item.get("head_sha") == head
            and str(record(item.get("app")).get("id")) == self.principal_id
            and item.get("name") == NAME
            and item.get("external_id") == slot
        ]

    @staticmethod
    def object(item):
        return {
            "id": native_id(item.get("id")),
            "metadata": metadata_from(record(item.get("output")).get("summary")),
        }

    def write(self, report, metadata, identifier=None):
        summary = summary_text(report, metadata)
        body = {
            "name": NAME,
            "status": "completed",
            "conclusion": "neutral",
            "external_id": metadata["slot"],
            "output": {"title": "Cirra advisory review", "summary": summary},
        }
        route = self.prefix + "/check-runs"
        if identifier:
            route += "/" + identifier
        else:
            body["head_sha"] = report["snapshot"]["head_sha"]
        item, _ = self.http.request("PATCH" if identifier else "POST", route, body=body)
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("app"), dict)
            or not isinstance(item.get("output"), dict)
            or type(item.get("id")) is not int
            or not 1 <= item["id"] <= 2**63 - 1
        ):
            raise RemoteUncertain("GitHub returned a malformed write acknowledgement")
        if (
            str(item.get("app", {}).get("id")) != self.principal_id
            or item.get("name") != NAME
            or item.get("external_id") != metadata["slot"]
            or item.get("head_sha") != report["snapshot"]["head_sha"]
        ):
            raise RemoteUncertain("GitHub write acknowledgement failed ownership validation")
        return self.object(item)


class GitLabPublisher:
    def __init__(self, client, principal_id):
        self.client, self.principal_id = client, str(principal_id)
        number(int(self.principal_id), "publisher user ID", 1, 2**63 - 1, integral=True)

    def find(self, report, slot):
        found = pages(self.client.get, f"merge_requests/{report['snapshot']['change_id']}/notes")
        return [
            {"id": native_id(item.get("id")), "metadata": metadata_from(item.get("body"))}
            for item in found
            if str(record(item.get("author")).get("id")) == self.principal_id
            and not item.get("system")
            and (metadata_from(item.get("body")) or {}).get("slot") == slot
        ]

    def write(self, report, metadata, identifier=None):
        body = summary_text(report, metadata)
        item = self.client.note(int(report["snapshot"]["change_id"]), body, identifier)
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("author"), dict)
            or type(item.get("id")) is not int
            or not 1 <= item["id"] <= 2**63 - 1
        ):
            raise RemoteUncertain("GitLab returned a malformed write acknowledgement")
        if str(item.get("author", {}).get("id")) != self.principal_id or item.get("system"):
            raise RemoteUncertain("GitLab write acknowledgement failed ownership validation")
        return {"id": native_id(item.get("id")), "metadata": metadata_from(item.get("body"))}


def write_receipt(path, receipt):
    path.parent.mkdir(parents=True, exist_ok=True)
    history = path.parent / "history" / path.stem
    history.mkdir(parents=True, exist_ok=True)
    historical = history / f"{digest(receipt)}.json"
    content = canonical(receipt) + "\n"
    if not historical.exists():
        with historical.open("x", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def checkpoint(store, claim, path, receipt):
    with store.transaction() as connection:
        store._fence(connection, claim)
        write_receipt(path, receipt)


def summary_text(report, metadata):
    body = marker(metadata) + "\n\n" + render_markdown(report)
    if len(body.encode()) > 60000:
        raise CirraError("native summary exceeds 60 KiB; reduce review limits")
    return body


def publish(
    store,
    config,
    run_id,
    collector,
    publisher,
    *,
    reconcile=False,
    retry_uncertain=False,
    canonical_id=None,
):
    """Refresh with the reader and serialize only this change across remote I/O."""
    original = store.latest_report(run_id)
    snap = original["snapshot"]
    change_key = digest(
        [snap[key] for key in ("provider", "instance", "repository_id", "change_id")]
    )
    with store.lease(change_key, config.claim_ttl_seconds, config.wait_seconds) as claim:
        provider = collector()
        if (
            provider.snapshot.record() != snap
            or original["inputs"]["config_digest"] != config.digest
        ):
            raise CirraError(
                "report snapshot or policy is obsolete; review the current change first"
            )
        order = store.latest_attempt_order(claim, run_id)
        report = make_report(
            provider.snapshot,
            assess_checks(provider.checks, provider.snapshot, config, provider.check_errors),
            original["review"],
            original["inputs"],
            run_id,
            config,
            collection=provider.collection,
        )
        store.save_report(claim, report)
        keys = ("provider", "instance", "repository_id", "change_id")
        if snap["provider"] != "gitlab":
            keys += ("head_sha",)
        slot = digest(
            {
                **{key: snap[key] for key in keys},
                "publisher_id": publisher.principal_id,
                "report_kind": NAME,
            }
        )
        path = store.path.parent / "publication" / f"{slot}.json"
        receipt = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        selected_id = canonical_id or (receipt or {}).get("canonical_id")
        found = publisher.find(report, slot)
        if selected_id:
            found = [item for item in found if item["id"] == selected_id]
            if not found and canonical_id:
                raise CirraError("canonical ID does not select one verified owned publication")
        if len(found) > 1:
            raise CirraError(
                "multiple owned publications found; select --canonical-id after inspection"
            )
        remote = found[0] if found else None
        if remote:
            metadata = remote["metadata"]
            if not isinstance(metadata, dict) or metadata.get("slot") != slot:
                raise CirraError(
                    "owned publication has invalid metadata; inspect it before retrying"
                )
            for field in ("run_order", "generation"):
                number(metadata.get(field), "publication " + field, 1, 2**63 - 1, integral=True)
            if metadata["run_order"] > order:
                raise CirraError("remote publication supersedes this local review")
        if receipt and receipt["state"] == "uncertain":
            if remote and remote["metadata"] == receipt["metadata"]:
                receipt.update(
                    state="published",
                    native_id=remote["id"],
                    canonical_id=selected_id,
                    acknowledged_at=utc_now(),
                )
                checkpoint(store, claim, path, receipt)
                return receipt
            if not retry_uncertain:
                reason = (
                    "Known-object update remains unconfirmed; inspect and use --retry-uncertain "
                    "to retry its ID. An older read does not prove the write failed."
                    if remote and receipt.get("native_id") == remote["id"]
                    else "Create unconfirmed; reconcile or inspect before --retry-uncertain."
                )
                return {**receipt, "reason": reason}
        if receipt and receipt["state"] == "published" and not remote:
            receipt.update(
                state="uncertain",
                reason="Saved object not visible; inspect before --retry-uncertain replaces it.",
            )
            checkpoint(store, claim, path, receipt)
            if not retry_uncertain:
                return receipt
        if reconcile and not retry_uncertain:
            if remote:
                result = {
                    "state": "published",
                    "native_id": remote["id"],
                    "canonical_id": selected_id,
                    "metadata": remote["metadata"],
                    "acknowledged_at": utc_now(),
                }
                checkpoint(store, claim, path, result)
                return result
            return {
                "state": "unresolved",
                "reason": "No unique owned publication is visible; no write attempted.",
            }
        generation = (
            max(
                (receipt or {}).get("metadata", {}).get("generation", 0),
                (remote or {}).get("metadata", {}).get("generation", 0),
            )
            + 1
        )
        metadata = {
            "slot": slot,
            "generation": generation,
            "run_order": order,
            "run_id": run_id,
            "head_sha": snap["head_sha"],
            "target_sha": snap["target_sha"],
            "diff_base_sha": snap["diff_base_sha"],
            "analysis_digest": report["analysis_digest"],
            "report_digest": report["report_digest"],
        }
        summary_text(report, metadata)  # A local size refusal is never an uncertain remote write.
        receipt = {
            "schema_version": 1,
            "state": "uncertain",
            "metadata": metadata,
            "native_id": remote["id"] if remote else None,
            "canonical_id": selected_id if remote else None,
            "intent_at": utc_now(),
            "duplicate_risk_accepted": bool(retry_uncertain and not remote),
        }
        checkpoint(store, claim, path, receipt)
        try:
            result = publisher.write(report, metadata, receipt["native_id"])
            if not isinstance(result, dict) or result.get("metadata") != metadata:
                raise RemoteUncertain("native publication metadata does not match the intent")
        except RemoteUncertain as exc:
            receipt["reason"] = str(exc)
        except CirraError as exc:
            receipt.update(state="failed", reason=str(exc))
        else:
            receipt.update(state="published", native_id=result["id"], acknowledged_at=utc_now())
        checkpoint(store, claim, path, receipt)
        return receipt
