"""Production-audit policy and tier selection; selection is pure and never asks a model."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from cirra.domain import CirraError, fields, number, path_name, string, timestamp

TIERS = ("release", "pr", "full")
SEVERITIES = ("info", "low", "medium", "high", "critical")
DEFAULT_GATES = {"pr": "critical", "release": "high", "full": "none"}

# Changes to these files can alter what the project is, which the diff-scoped
# pr tier cannot assess, so they also queue a full audit.
DEFAULT_ESCALATE_PATHS = (
    # Manifests and lockfiles
    "**/pyproject.toml",
    "**/setup.py",
    "**/setup.cfg",
    "**/requirements*.txt",
    "**/requirements/**",
    "**/package.json",
    "**/package-lock.json",
    "**/pnpm-lock.yaml",
    "**/*.lock",
    "**/go.mod",
    "**/go.sum",
    "**/Cargo.toml",
    "**/Gemfile",
    "**/pom.xml",
    "**/build.gradle",
    "**/build.gradle.kts",
    # Containers
    "**/Dockerfile",
    "**/Dockerfile.*",
    "**/*.dockerfile",
    "**/docker-compose*.yml",
    "**/docker-compose*.yaml",
    "**/compose.yml",
    "**/compose.yaml",
    # Deployment and unit files
    "deploy/**",
    "**/*.service",
    "**/*.timer",
    "**/*.socket",
    # Migrations
    "**/migrations/**",
    # CI workflow definitions
    ".github/workflows/**",
    ".gitlab-ci.yml",
    ".forgejo/workflows/**",
    ".gitea/workflows/**",
    # Infrastructure as code
    "**/*.tf",
    "**/*.tfvars",
)


@dataclass(frozen=True)
class AuditPolicy:
    tiers: tuple[str, ...]
    runner_kind: str
    runner_command: tuple[str, ...] | None
    schedule_days: int
    max_age_days: int
    escalate_paths: tuple[str, ...]
    gates: dict
    debounce_seconds: float
    audit_timeout_seconds: float
    max_audits_per_day: int


def _patterns(value, label):
    if not isinstance(value, list) or len(value) > 256:
        raise CirraError(f"{label} must be a list of at most 256 path patterns")
    return tuple(path_name(pattern) for pattern in value)


def load_audit_policy(data):
    audit = fields(
        data,
        ["tiers"],
        ["runner", "full", "escalate_paths", "escalate_paths_add", "gates", "limits"],
        label="audit",
    )
    tiers = audit["tiers"]
    if (
        not isinstance(tiers, list)
        or not tiers
        or len(set(map(str, tiers))) != len(tiers)
        or any(tier not in TIERS for tier in tiers)
    ):
        raise CirraError("audit tiers must be a nonempty list of distinct pr, release, full")

    runner = fields(audit.get("runner", {"kind": "session"}), ["kind"], ["command"], label="runner")
    command = runner.get("command")
    if runner["kind"] == "session":
        if command is not None:
            raise CirraError("a session runner takes no command")
    elif runner["kind"] == "command":
        if not isinstance(command, list) or not command or len(command) > 64:
            raise CirraError("a command runner needs an argv list, never a shell string")
        command = tuple(string(part, "runner argument") for part in command)
    else:
        raise CirraError("audit runner kind must be session or command")

    full = fields(audit.get("full", {}), [], ["schedule_days", "max_age_days"], label="audit full")
    schedule_days = number(full.get("schedule_days", 7), "schedule_days", 1, 365, integral=True)
    max_age_days = number(full.get("max_age_days", 14), "max_age_days", 1, 3650, integral=True)
    if max_age_days < schedule_days:
        raise CirraError("max_age_days must be at least schedule_days")

    if "escalate_paths" in audit and "escalate_paths_add" in audit:
        raise CirraError("use either escalate_paths (replace) or escalate_paths_add (extend)")
    if "escalate_paths" in audit:
        escalate = _patterns(audit["escalate_paths"], "escalate_paths")
    else:
        escalate = DEFAULT_ESCALATE_PATHS + _patterns(
            audit.get("escalate_paths_add", []), "escalate_paths_add"
        )

    gates = {**DEFAULT_GATES, **fields(audit.get("gates", {}), [], TIERS, label="audit gates")}
    for tier, level in gates.items():
        if level not in ("none", *SEVERITIES):
            raise CirraError(f"audit gate {tier} must be none or a finding severity")
    if gates["full"] != "none":
        raise CirraError("full audits never block; gates.full must be none")

    limits = fields(
        audit.get("limits", {}),
        [],
        ["debounce_seconds", "audit_timeout_seconds", "max_audits_per_day"],
        label="audit limits",
    )
    return AuditPolicy(
        tiers=tuple(tiers),
        runner_kind=runner["kind"],
        runner_command=command,
        schedule_days=schedule_days,
        max_age_days=max_age_days,
        escalate_paths=escalate,
        gates=gates,
        debounce_seconds=number(limits.get("debounce_seconds", 120), "debounce_seconds", 0, 3600),
        audit_timeout_seconds=number(
            limits.get("audit_timeout_seconds", 3600), "audit_timeout_seconds", 60, 86400
        ),
        max_audits_per_day=number(
            limits.get("max_audits_per_day", 20), "max_audits_per_day", 1, 10000, integral=True
        ),
    )


def _glob(pattern):
    """`**` spans directories (including none), `*` and `?` stay within one."""
    parts = pattern.split("/")
    regex = ""
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "**":
            regex += ".*" if last else "(?:[^/]+/)*"
            continue
        regex += "".join(
            "[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char) for char in part
        )
        regex += "" if last else "/"
    return re.compile(regex)


def structural(policy, paths):
    patterns = [_glob(pattern) for pattern in policy.escalate_paths]
    return any(pattern.fullmatch(path) for path in paths for pattern in patterns)


def _event(event):
    if not isinstance(event, dict):
        raise CirraError("audit event must be an object")
    shapes = {
        "change": ["kind", "changed_paths"],
        "release": ["kind"],
        "registered": ["kind"],
        "schedule": ["kind"],
        "operator": ["kind", "tier"],
    }
    if event.get("kind") not in shapes:
        raise CirraError("unsupported audit event kind")
    fields(event, shapes[event["kind"]], label="audit event")
    if event["kind"] == "change":
        paths = event["changed_paths"]
        if not isinstance(paths, list):
            raise CirraError("changed_paths must be a list")
        for path in paths:
            path_name(path)
    if event["kind"] == "operator" and event["tier"] not in TIERS:
        raise CirraError("operator audit tier must be pr, release, or full")
    return event


def _full_reason(policy, event, latest_full, catalog_digest, now):
    if latest_full is None:
        return "baseline"
    fields(latest_full, ["completed_at", "catalog_digest"], label="stored full audit")
    if not isinstance(latest_full["catalog_digest"], str) or not re.fullmatch(
        r"[a-f0-9]{64}", latest_full["catalog_digest"]
    ):
        raise CirraError("stored catalog_digest must be a lowercase SHA-256 digest")
    if latest_full["catalog_digest"] != catalog_digest:
        return "catalog"
    age = max(now - datetime.fromisoformat(timestamp(latest_full["completed_at"])), timedelta(0))
    if age > timedelta(days=policy.max_age_days):
        return "stale"
    if event["kind"] == "change" and structural(policy, event["changed_paths"]):
        return "structural"
    if event["kind"] == "schedule" and age >= timedelta(days=policy.schedule_days):
        return "schedule"
    return None


def select_tiers(policy, event, latest_full, *, catalog_digest, now):
    """Map one event and the newest stored full audit to ordered (tier, reason) pairs.

    `latest_full` is None or {"completed_at", "catalog_digest"}; `catalog_digest`
    is the catalog Cirra currently validates against. Output order is run
    priority: release, then pr, then full. Disabled tiers are never returned.
    """
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise CirraError("selection time must carry a timezone")
    if not isinstance(catalog_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", catalog_digest):
        raise CirraError("catalog_digest must be a lowercase SHA-256 digest")
    if policy is None:
        return ()
    event = _event(event)
    if event["kind"] == "operator":
        if event["tier"] not in policy.tiers:
            raise CirraError(f"audit tier {event['tier']} is not enabled in the policy")
        return ((event["tier"], "operator"),)
    selected = []
    if event["kind"] == "release":
        selected.append(("release", "release"))
    if event["kind"] == "change":
        selected.append(("pr", "change"))
    reason = _full_reason(policy, event, latest_full, catalog_digest, now)
    if reason:
        selected.append(("full", reason))
    return tuple((tier, why) for tier, why in selected if tier in policy.tiers)
