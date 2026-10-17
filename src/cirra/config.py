"""Load explicitly selected operator policy; never discover policy inside a change."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from cirra.audit import AuditPolicy, load_audit_policy
from cirra.domain import CirraError, fields, number, string


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in mapping:
            raise CirraError("policy keys must be unique strings")
        mapping[key] = loader.construct_object(value_node)
    return mapping


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


@dataclass(frozen=True)
class Config:
    path: str
    digest: str
    repositories: tuple[dict, ...]
    ci_mode: str
    expected_checks: tuple[dict, ...]
    reviewer_kind: str
    reviewer_endpoint: str
    model: str
    max_diff_bytes: int
    max_response_bytes: int
    max_findings: int
    claim_ttl_seconds: float
    wait_seconds: float
    review_timeout_seconds: float = 180
    review_budget_usd: float = 1
    audit: AuditPolicy | None = None


def load_config(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise CirraError("operator policy exceeds 64 KiB")
    try:
        data = yaml.load(raw.decode("utf-8"), Loader=UniqueLoader)
    except (yaml.YAMLError, UnicodeError, RecursionError) as exc:
        raise CirraError("operator policy is not valid safe YAML") from exc
    fields(data, ["version", "repositories", "ci", "reviewer", "limits"], ["audit"], label="policy")
    if type(data["version"]) is not int or data["version"] != 1:
        raise CirraError("only policy version 1 is supported")
    repositories = data["repositories"]
    if not isinstance(repositories, list) or not repositories:
        raise CirraError("repositories must be an explicit nonempty allowlist")
    for repo in repositories:
        fields(repo, ["provider", "instance", "repository_id"], label="repository")
        for name, value in repo.items():
            string(value, name)
    ci = fields(data["ci"], ["mode", "expected"], label="ci")
    if ci["mode"] not in ("required", "review_only") or not isinstance(ci["expected"], list):
        raise CirraError("ci requires mode required/review_only and an expected list")
    if bool(ci["expected"]) != (ci["mode"] == "required"):
        raise CirraError("required mode needs checks; an empty list requires review_only mode")
    identities = set()
    for check in ci["expected"]:
        fields(check, ["kind", "source_id", "name"], ["definition_digest"], label="expected check")
        if "definition_digest" in check and not re.fullmatch(
            r"[a-f0-9]{64}", str(check["definition_digest"])
        ):
            raise CirraError("CI definition_digest must be a lowercase SHA-256 digest")
        if check["kind"] not in ("check_run", "commit_status", "pipeline_job", "observer_run"):
            raise CirraError("unsupported expected check kind")
        if check["kind"] == "observer_run" and (
            check["source_id"] != "cirra/docker" or "definition_digest" not in check
        ):
            raise CirraError("local observer checks must pin cirra/docker and its definition")
        for name, value in check.items():
            string(value, name)
        if check["source_id"] in ("not_exposed", "unknown"):
            raise CirraError("expected checks need an exposed native source identity")
        identity = (check["kind"], check["source_id"], check["name"])
        if identity in identities or check["name"] == "cirra/review":
            raise CirraError("duplicate expected check or Cirra's own output in policy")
        identities.add(identity)
    reviewer = fields(data["reviewer"], ["kind", "endpoint", "model"], label="reviewer")
    endpoints = {"fake": "fake://local", "session": "session://manual", "claude": "cli://claude"}
    if not isinstance(reviewer["kind"], str) or reviewer["kind"] not in endpoints:
        raise CirraError("reviewer kind must be fake, session, or claude")
    if reviewer["endpoint"] != endpoints[reviewer["kind"]]:
        raise CirraError("reviewer endpoint does not match the selected kind")
    string(reviewer["model"], "model")
    limits = fields(
        data["limits"],
        [
            "max_diff_bytes",
            "max_response_bytes",
            "max_findings",
            "claim_ttl_seconds",
            "wait_seconds",
        ],
        ["review_timeout_seconds", "review_budget_usd"],
        label="limits",
    )
    for name in ("max_diff_bytes", "max_response_bytes"):
        number(limits[name], name, 1, 8 * 1024 * 1024, integral=True)
    number(limits["max_findings"], "max_findings", 0, 1000, integral=True)
    number(limits["claim_ttl_seconds"], "claim_ttl_seconds", 0.2, 600)
    number(limits["wait_seconds"], "wait_seconds", 0, 600)
    if "review_timeout_seconds" in limits:
        number(limits["review_timeout_seconds"], "review_timeout_seconds", 1, 900)
    if "review_budget_usd" in limits:
        number(limits["review_budget_usd"], "review_budget_usd", 0.01, 20)
    return Config(
        path=str(path),
        digest=hashlib.sha256(raw).hexdigest(),
        repositories=tuple(repositories),
        ci_mode=ci["mode"],
        expected_checks=tuple(ci["expected"]),
        reviewer_kind=reviewer["kind"],
        reviewer_endpoint=reviewer["endpoint"],
        model=reviewer["model"],
        **limits,
        audit=load_audit_policy(data["audit"]) if "audit" in data else None,
    )
