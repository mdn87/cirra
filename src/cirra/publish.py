"""Explicit advisory writes with durable intents, ownership checks and reconciliation."""

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from cirra.domain import CirraError, canonical, digest, fields, number, timestamp, utc_now
from cirra.github import API_VERSION, check_summary_digest, native_id, record, repository_name
from cirra.policy import assess_checks
from cirra.publication_fence import PublicationFence, require_send, require_state_owner
from cirra.report import make_report, render_markdown
from cirra.transport import JSONClient, RemoteUncertain, pages

NAME = "cirra/review"
TITLE = "Cirra advisory review"


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
            "body_digest": check_summary_digest(item, TITLE)
            if item.get("status") == "completed" and item.get("conclusion") == "neutral"
            else None,
        }

    def write(self, report, metadata, identifier=None):
        summary = summary_text(report, metadata)
        require_send("advisory", metadata["slot"], digest(summary), identifier)
        body = {
            "name": NAME,
            "status": "completed",
            "conclusion": "neutral",
            "external_id": metadata["slot"],
            "output": {"title": TITLE, "summary": summary},
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
            or (identifier is not None and str(item["id"]) != identifier)
        ):
            raise RemoteUncertain("GitHub write acknowledgement failed ownership validation")
        result = self.object(item)
        if result["body_digest"] != digest(summary):
            raise RemoteUncertain("GitHub check output does not match the advisory write")
        return result


class GitLabPublisher:
    def __init__(self, client, principal_id):
        self.client, self.principal_id = client, str(principal_id)
        number(int(self.principal_id), "publisher user ID", 1, 2**63 - 1, integral=True)

    def find(self, report, slot):
        found = pages(self.client.get, f"merge_requests/{report['snapshot']['change_id']}/notes")
        return [
            {
                "id": native_id(item.get("id")),
                "metadata": metadata_from(item.get("body")),
                "body_digest": digest(item["body"]),
            }
            for item in found
            if str(record(item.get("author")).get("id")) == self.principal_id
            and not item.get("system")
            and (metadata_from(item.get("body")) or {}).get("slot") == slot
        ]

    def write(self, report, metadata, identifier=None):
        body = summary_text(report, metadata)
        require_send("advisory", metadata.get("slot"), digest(body), identifier)
        item = self.client.note(int(report["snapshot"]["change_id"]), body, identifier)
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("author"), dict)
            or type(item.get("id")) is not int
            or not 1 <= item["id"] <= 2**63 - 1
        ):
            raise RemoteUncertain("GitLab returned a malformed write acknowledgement")
        if (
            str(item.get("author", {}).get("id")) != self.principal_id
            or item.get("system")
            or (identifier is not None and str(item["id"]) != identifier)
        ):
            raise RemoteUncertain("GitLab write acknowledgement failed ownership validation")
        if item.get("body") != body:
            raise RemoteUncertain("GitLab note body does not match the advisory write")
        return {"id": native_id(item["id"]), "metadata": metadata, "body_digest": digest(body)}


def sync_directory(path):
    """Commit directory entries on POSIX after moving a durable receipt into place."""
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def receipt_directory(path):
    if not path.is_dir():
        receipt_directory(path.parent)
        path.mkdir(exist_ok=True)
        sync_directory(path.parent)


def replace_receipt(path, content):
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def write_receipt(path, receipt, *, history=True):
    """Atomically replace `path`, keeping every distinct version as evidence.

    `history=False` is only for overwrite-in-place state such as heartbeats,
    whose every write differs and would otherwise grow history without bound.
    """
    receipt_directory(path.parent)
    content = canonical(receipt) + "\n"
    if history:
        versions = path.parent / "history" / path.stem
        receipt_directory(versions)
        historical = versions / f"{digest(receipt)}.json"
        if not historical.exists():
            replace_receipt(historical, content)
    replace_receipt(path, content)
    if path.parent.name in ("audit-publication", "audit-waivers"):
        # Give a read-only observer a collision-free invalidation signal even
        # when successive atomic replacements share a filesystem timestamp.
        replace_receipt(path.parent / ".revision", uuid.uuid4().hex)


def checkpoint(store, claim, path, receipt):
    with store.transaction() as connection:
        store._fence(connection, claim)
        write_receipt(path, receipt)


def summary_text(report, metadata):
    body = marker(metadata) + "\n\n" + render_markdown(report)
    if len(body.encode()) > 60000:
        raise CirraError("native summary exceeds 60 KiB; reduce review limits")
    return body


def without_observed_at(record):
    return {key: value for key, value in record.items() if key != "observed_at"}


def coordinated_content_digest(report, native_check=None, *, legacy=False):
    content = {k: v for k, v in report.items() if k not in ("observed_at", "report_digest")}
    if legacy:
        return digest(content)
    if isinstance(content.get("collection"), dict):
        content["collection"] = without_observed_at(content["collection"])
    # The collector stamps both copies of a CI observation. Keep native times,
    # provenance, and identities. Only the actual advisory returned for this slot
    # can disappear from the comparison when a subsequent collection sees it.
    ci = dict(content["ci"])
    if "evidence" in ci:
        ci["evidence"] = [
            without_observed_at(check)
            for check in ci["evidence"]
            if not (
                native_check
                and check.get("excluded") is True
                and check.get("name") == NAME
                and check.get("kind") == "check_run"
                and (check.get("source_id"), check.get("native_id"), check.get("associated_sha"))
                == native_check
            )
        ]
    if "expected" in ci:
        ci["expected"] = [
            {**expected, "evidence": without_observed_at(expected["evidence"])}
            if isinstance(expected.get("evidence"), dict)
            else expected
            for expected in ci["expected"]
        ]
    content["ci"] = ci
    return digest(content)


def matches_coordinated_content(store, metadata, current, native_check=None):
    previous = metadata.get("content_digest")
    if previous == current:
        return True
    if not isinstance(previous, str) or not all(
        isinstance(metadata.get(k), str) for k in ("run_id", "report_digest")
    ):
        return False
    # Earlier markers included the collection timestamp. Reuse an authenticated matching
    # publication only when its exact historical report proves both forms of the digest.
    saved = store.report_by_digest(metadata.get("run_id"), metadata.get("report_digest"))
    return (
        saved is not None
        and coordinated_content_digest(saved, legacy=True) == previous
        and coordinated_content_digest(saved, native_check) == current
    )


def matches_saved_body(store, remote, receipt, snapshot):
    if not remote.get("body_digest"):
        return False
    metadata = remote["metadata"]
    if receipt and metadata == receipt["metadata"] and receipt.get("body_digest"):
        return remote["body_digest"] == receipt["body_digest"]
    # Older receipts and restored directories may only identify the historical report.
    # A later CI refresh can change the Markdown for the same run.
    if not all(isinstance(metadata.get(key), str) for key in ("run_id", "report_digest")):
        return False
    historical = store.report_by_digest(metadata["run_id"], metadata["report_digest"])
    if historical is None or historical["analysis_digest"] != metadata.get("analysis_digest"):
        return False
    saved_snapshot = historical["snapshot"]
    identity = ("provider", "instance", "repository_id", "change_id")
    if snapshot["provider"] != "gitlab":
        identity += ("head_sha",)
    if any(saved_snapshot[k] != snapshot[k] for k in identity) or any(
        saved_snapshot[k] != metadata.get(k) for k in ("head_sha", "target_sha", "diff_base_sha")
    ):
        return False
    # Receipts without a body digest can predate the "Next step" Markdown format.
    # A check carrying a new operation marker was never written in that older format.
    formats = (False,) if "operation_id" in metadata else (True, False)
    prefix = marker(metadata) + "\n\n"
    return any(
        digest(prefix + render_markdown(historical, legacy_format=legacy)) == remote["body_digest"]
        for legacy in formats
    )


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
    require_state_owner(store.path.parent)
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
        fence = PublicationFence("advisory", slot)
        if fence.owner is not None and type(publisher) not in (GitHubPublisher, GitLabPublisher):
            raise CirraError("shared publication requires a supported native advisory writer")
        if fence.owner is not None and (retry_uncertain or canonical_id):
            raise CirraError("shared publication cannot override an uncertain native slot")
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
        native_check = (
            (publisher.principal_id, remote["id"], snap["head_sha"])
            if remote and snap["provider"] == "github"
            else None
        )
        content_digest = (
            coordinated_content_digest(report, native_check) if fence.owner is not None else None
        )
        known = (
            fence.observe(remote["id"], remote.get("body_digest"))
            if remote
            else fence.observe(None, None)
        )
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
            if fence.owner is not None and metadata.get("run_id") != run_id:
                # A restored SQLite database can assign the same rowid (or a higher
                # one) on a divergent history. Require the previous report locally
                # before letting a different run replace the provider's current one.
                if metadata["run_order"] == order or not store.report_by_digest(
                    metadata.get("run_id"), metadata.get("report_digest")
                ):
                    raise CirraError("private review history cannot order these publication runs")
            if (
                known
                and matches_coordinated_content(store, metadata, content_digest, native_check)
                and (
                    not receipt
                    or (
                        receipt.get("native_id") in (None, remote["id"])
                        and (
                            metadata["generation"] > receipt["metadata"]["generation"]
                            or (receipt["state"] == "published" and receipt["metadata"] == metadata)
                        )
                    )
                )
            ):
                # The worker's private snapshot can predate its predecessor's send.
                # Quorum and the owned native object agree on this exact output.
                result = {
                    "state": "published",
                    "native_id": remote["id"],
                    "canonical_id": selected_id,
                    "metadata": metadata,
                    "body_digest": remote["body_digest"],
                    "acknowledged_at": utc_now(),
                }
                checkpoint(store, claim, path, result)
                return result
            if (
                not retry_uncertain
                and (not receipt or metadata["generation"] > receipt["metadata"]["generation"])
                and not known
                and not matches_saved_body(store, remote, receipt, snap)
            ):
                # A restore can see a later native write than the local snapshot. Keep
                # its ID even if the next provider read temporarily returns no object.
                if not receipt or receipt["state"] != "uncertain":
                    receipt = {
                        "state": "uncertain",
                        "native_id": remote["id"],
                        "canonical_id": selected_id,
                        "metadata": {**metadata, "awaiting_ack": True},
                        "reason": "The visible output does not match a saved report.",
                    }
                    checkpoint(store, claim, path, receipt)
                return receipt
        if receipt and receipt["state"] == "uncertain":
            expected = dict(receipt["metadata"])
            expected.pop("awaiting_ack", None)
            if (
                remote
                and remote["metadata"] == expected
                and (receipt.get("native_id") is None or receipt["native_id"] == remote["id"])
                and (
                    known
                    or matches_saved_body(store, remote, {**receipt, "metadata": expected}, snap)
                )
            ):
                receipt.pop("reason", None)
                receipt.update(
                    state="published",
                    native_id=remote["id"],
                    canonical_id=selected_id,
                    metadata=expected,
                    body_digest=remote["body_digest"],
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
        if (
            receipt
            and receipt["state"] == "published"
            and not canonical_id
            and (
                not remote
                or receipt["native_id"] != remote["id"]
                or (
                    remote["metadata"]["generation"] <= receipt["metadata"]["generation"]
                    and (
                        remote["metadata"] != receipt["metadata"]
                        or not (known or matches_saved_body(store, remote, receipt, snap))
                    )
                )
            )
        ):
            receipt.update(
                state="uncertain",
                metadata={**receipt["metadata"], "awaiting_ack": True},
                reason="The confirmed publication ID and output are not visible together.",
            )
            checkpoint(store, claim, path, receipt)
            if not retry_uncertain:
                return receipt
        if reconcile and not retry_uncertain:
            if remote and (known or matches_saved_body(store, remote, receipt, snap)):
                result = {
                    "state": "published",
                    "native_id": remote["id"],
                    "canonical_id": selected_id,
                    "metadata": remote["metadata"],
                    "body_digest": remote["body_digest"],
                    "acknowledged_at": utc_now(),
                }
                checkpoint(store, claim, path, result)
                return result
            return {
                "state": "unresolved",
                "reason": "No unique owned publication matches a saved report; no write attempted.",
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
            "operation_id": uuid.uuid4().hex,
            "run_order": order,
            "run_id": run_id,
            "head_sha": snap["head_sha"],
            "target_sha": snap["target_sha"],
            "diff_base_sha": snap["diff_base_sha"],
            "analysis_digest": report["analysis_digest"],
            "report_digest": report["report_digest"],
        }
        if fence.owner is not None:
            metadata["content_digest"] = content_digest
        body_digest = digest(summary_text(report, metadata))
        receipt = {
            "schema_version": 1,
            "state": "uncertain",
            # Older workers compare this whole mapping. Until exact readback they must
            # keep even a visible object parked when rolling back to their marker-only check.
            "metadata": {**metadata, "awaiting_ack": True},
            "body_digest": body_digest,
            "native_id": remote["id"] if remote else None,
            "canonical_id": selected_id if remote else None,
            "intent_at": utc_now(),
            "duplicate_risk_accepted": bool(retry_uncertain and not remote),
        }
        staged = False
        try:
            with fence.writing(body_digest, receipt["native_id"]):
                checkpoint(store, claim, path, receipt)
                staged = True
                result = publisher.write(report, metadata, receipt["native_id"])
                if (
                    not isinstance(result, dict)
                    or result.get("metadata") != metadata
                    or result.get("body_digest") != body_digest
                    or (
                        receipt["native_id"] is not None
                        and result.get("id") != receipt["native_id"]
                    )
                ):
                    raise RemoteUncertain("native publication does not match the saved write")
                fence.acknowledged(body_digest, result["id"], target=receipt["native_id"])
        except RemoteUncertain as exc:
            if not staged:
                raise
            receipt["reason"] = str(exc)
        except CirraError as exc:
            if not staged:
                raise
            receipt.update(state="failed", metadata=metadata, reason=str(exc))
        else:
            receipt.update(
                state="published",
                native_id=result["id"],
                metadata=metadata,
                acknowledged_at=utc_now(),
            )
        checkpoint(store, claim, path, receipt)
        return receipt
