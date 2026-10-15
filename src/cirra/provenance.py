"""Verify statements issued by an explicitly trusted checkout witness.

This authenticates the witness, not the truth of arbitrary CI artifacts. The
witness must observe checkout outside the control of the tested code. Never
provide its signing key to a PR workflow or reviewer.
"""

import copy
import hashlib
import hmac
import json
from pathlib import Path

from cirra.domain import CirraError, canonical, digest, fields, sha, string, timestamp
from cirra.reviewer import unique_object


def binding(change, check):
    return digest(
        {
            "repository": {key: change[key] for key in ("provider", "instance", "repository_id")},
            "check": {
                key: check[key]
                for key in (
                    "kind",
                    "source_id",
                    "name",
                    "native_id",
                    "run_id",
                    "attempt",
                    "associated_sha",
                    "started_at",
                    "raw_state",
                )
            },
        }
    )


def validate_statement(statement):
    fields(statement, ["version", "witness", "observed_at", "checks"], label="witness statement")
    if type(statement["version"]) is not int or statement["version"] != 1:
        raise CirraError("unsupported witness statement version")
    string(statement["witness"], "witness identity", 200)
    timestamp(statement["observed_at"])
    if not isinstance(statement["checks"], list) or len(statement["checks"]) > 1000:
        raise CirraError("witness checks must be a bounded list")
    seen = set()
    for item in statement["checks"]:
        fields(
            item,
            ["binding", "tested_sha", "parents", "evidence"],
            ["definition_digest"],
            label="witness check",
        )
        if "definition_digest" in item:
            value = string(item["definition_digest"], "CI definition digest", 64)
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise CirraError("CI definition digest must be lowercase SHA-256")
        if (
            not isinstance(item["binding"], str)
            or len(item["binding"]) != 64
            or item["binding"] in seen
        ):
            raise CirraError("invalid or duplicate witness binding")
        seen.add(item["binding"])
        sha(item["tested_sha"])
        if not isinstance(item["parents"], list) or len(item["parents"]) > 16:
            raise CirraError("invalid witness parent list")
        for parent in item["parents"]:
            sha(parent)
        string(item["evidence"], "independent witness evidence")
    return statement


def sign(statement, key):
    validate_statement(statement)
    if not isinstance(key, str) or len(key.encode()) < 32:
        raise CirraError(
            "witness key must contain at least 32 bytes of independent secret material"
        )
    return {
        "statement": statement,
        "hmac_sha256": hmac.new(
            key.encode(), canonical(statement).encode(), hashlib.sha256
        ).hexdigest(),
    }


def apply(data, path, key):
    with Path(path).open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise CirraError("witness receipt exceeds 1 MiB")
    try:
        receipt = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise CirraError("invalid witness receipt JSON") from None
    fields(receipt, ["statement", "hmac_sha256"], label="signed witness receipt")
    expected = sign(receipt["statement"], key)["hmac_sha256"]
    if not isinstance(receipt["hmac_sha256"], str) or not hmac.compare_digest(
        expected, receipt["hmac_sha256"]
    ):
        raise CirraError("checkout witness signature does not match the selected trusted key")
    data = copy.deepcopy(data)
    witnesses = {item["binding"]: item for item in receipt["statement"]["checks"]}
    for check in data["checks"]:
        item = witnesses.get(binding(data["change"], check))
        if item:
            check["provenance"] = {
                "trusted": True,
                "tested_sha": item["tested_sha"],
                "parents": item["parents"],
                "witness": receipt["statement"]["witness"],
                "receipt_digest": digest(receipt),
                "evidence": item["evidence"],
                **(
                    {"definition_digest": item["definition_digest"]}
                    if "definition_digest" in item
                    else {}
                ),
            }
    return data
