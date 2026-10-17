"""Stage 1 of the production-audit tiers plan: audit policy and tier selection."""

from datetime import UTC, datetime, timedelta

import pytest
import yaml

from cirra.audit import DEFAULT_ESCALATE_PATHS, select_tiers
from cirra.config import load_config
from cirra.domain import CirraError

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
CATALOG = "a" * 64
OLD_CATALOG = "b" * 64

AUDIT = {
    "tiers": ["pr", "release", "full"],
    "runner": {"kind": "session", "command": None},
    "full": {"schedule_days": 7, "max_age_days": 14},
    "gates": {"pr": "critical", "release": "high", "full": "none"},
    "limits": {"debounce_seconds": 120, "audit_timeout_seconds": 3600, "max_audits_per_day": 20},
}


def load(tmp_path, policy_data, audit):
    policy = dict(policy_data)
    if audit is not None:
        policy["audit"] = audit
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    return load_config(path)


def full_audit(days_ago, catalog=CATALOG):
    completed = NOW - timedelta(days=days_ago)
    return {"completed_at": completed.isoformat(), "catalog_digest": catalog}


def change(*paths):
    return {"kind": "change", "changed_paths": list(paths)}


# One row per line of the plan's tier table, plus the edges each row implies.
SELECTION = [
    ("change opened or updated", change("src/app.py"), full_audit(1), [("pr", "change")]),
    (
        "change touching a manifest",
        change("src/app.py", "pyproject.toml"),
        full_audit(1),
        [("pr", "change"), ("full", "structural")],
    ),
    (
        "change touching a nested Dockerfile",
        change("services/api/Dockerfile"),
        full_audit(1),
        [("pr", "change"), ("full", "structural")],
    ),
    (
        "change touching a CI workflow",
        change(".github/workflows/ci.yml"),
        full_audit(1),
        [("pr", "change"), ("full", "structural")],
    ),
    (
        "change touching a migration",
        change("app/db/migrations/0002_users.py"),
        full_audit(1),
        [("pr", "change"), ("full", "structural")],
    ),
    ("release request", {"kind": "release"}, full_audit(1), [("release", "release")]),
    ("registration without a full audit", {"kind": "registered"}, None, [("full", "baseline")]),
    ("registration with a full audit", {"kind": "registered"}, full_audit(1), []),
    ("schedule tick, recent audit", {"kind": "schedule"}, full_audit(6), []),
    ("schedule tick, audit due", {"kind": "schedule"}, full_audit(7), [("full", "schedule")]),
    ("schedule tick, no audit", {"kind": "schedule"}, None, [("full", "baseline")]),
    (
        "full audit older than max_age_days",
        change("src/app.py"),
        full_audit(15),
        [("pr", "change"), ("full", "stale")],
    ),
    (
        "new catalog version",
        {"kind": "release"},
        full_audit(1, OLD_CATALOG),
        [("release", "release"), ("full", "catalog")],
    ),
    (
        "first change before any baseline",
        change("src/app.py"),
        None,
        [("pr", "change"), ("full", "baseline")],
    ),
    (
        "operator, named tier",
        {"kind": "operator", "tier": "release"},
        None,
        [("release", "operator")],
    ),
    ("operator, full", {"kind": "operator", "tier": "full"}, full_audit(1), [("full", "operator")]),
]


@pytest.mark.parametrize(
    ("event", "latest_full", "expected"),
    [row[1:] for row in SELECTION],
    ids=[row[0] for row in SELECTION],
)
def test_tier_table(tmp_path, policy_data, event, latest_full, expected):
    config = load(tmp_path, policy_data, AUDIT)
    assert select_tiers(config.audit, event, latest_full, catalog_digest=CATALOG, now=NOW) == tuple(
        expected
    )


def test_selection_is_deterministic(tmp_path, policy_data):
    config = load(tmp_path, policy_data, AUDIT)
    event = change("pyproject.toml")
    results = {
        select_tiers(config.audit, event, None, catalog_digest=CATALOG, now=NOW) for _ in range(5)
    }
    assert len(results) == 1


def test_disabled_tiers_are_never_selected(tmp_path, policy_data):
    config = load(tmp_path, policy_data, {"tiers": ["pr"]})
    assert select_tiers(
        config.audit, change("pyproject.toml"), None, catalog_digest=CATALOG, now=NOW
    ) == (("pr", "change"),)
    assert (
        select_tiers(config.audit, {"kind": "release"}, None, catalog_digest=CATALOG, now=NOW) == ()
    )
    with pytest.raises(CirraError, match="not enabled"):
        select_tiers(
            config.audit,
            {"kind": "operator", "tier": "full"},
            None,
            catalog_digest=CATALOG,
            now=NOW,
        )


def test_structural_escalation_without_pr_tier(tmp_path, policy_data):
    config = load(tmp_path, policy_data, {"tiers": ["full"]})
    assert select_tiers(
        config.audit, change("Dockerfile"), full_audit(1), catalog_digest=CATALOG, now=NOW
    ) == (("full", "structural"),)


def test_policy_paths_replace_or_extend_defaults(tmp_path, policy_data):
    replaced = load(
        tmp_path, policy_data, {"tiers": ["pr", "full"], "escalate_paths": ["infra/**"]}
    )
    assert replaced.audit.escalate_paths == ("infra/**",)
    assert select_tiers(
        replaced.audit, change("pyproject.toml"), full_audit(1), catalog_digest=CATALOG, now=NOW
    ) == (("pr", "change"),)
    assert select_tiers(
        replaced.audit, change("infra/net/main.tf"), full_audit(1), catalog_digest=CATALOG, now=NOW
    ) == (("pr", "change"), ("full", "structural"))

    extended = load(
        tmp_path,
        policy_data,
        {"tiers": ["pr", "full"], "escalate_paths_add": ["scripts/deploy-*.sh"]},
    )
    assert extended.audit.escalate_paths == DEFAULT_ESCALATE_PATHS + ("scripts/deploy-*.sh",)
    assert select_tiers(
        extended.audit,
        change("scripts/deploy-release.sh"),
        full_audit(1),
        catalog_digest=CATALOG,
        now=NOW,
    ) == (("pr", "change"), ("full", "structural"))


def test_single_star_stays_within_one_directory(tmp_path, policy_data):
    config = load(
        tmp_path, policy_data, {"tiers": ["pr", "full"], "escalate_paths": ["deploy/*.yml"]}
    )
    assert select_tiers(
        config.audit, change("deploy/a/b.yml"), full_audit(1), catalog_digest=CATALOG, now=NOW
    ) == (("pr", "change"),)


def test_policy_without_audit_block_loads_unchanged(tmp_path, policy_data):
    config = load(tmp_path, policy_data, None)
    assert config.audit is None
    assert (
        select_tiers(config.audit, change("pyproject.toml"), None, catalog_digest=CATALOG, now=NOW)
        == ()
    )


def test_defaults_fill_optional_sections(tmp_path, policy_data):
    audit = load(tmp_path, policy_data, {"tiers": ["pr"]}).audit
    assert audit.tiers == ("pr",)
    assert audit.runner_kind == "session" and audit.runner_command is None
    assert (audit.schedule_days, audit.max_age_days) == (7, 14)
    assert audit.gates == {"pr": "critical", "release": "high", "full": "none"}
    assert (audit.debounce_seconds, audit.audit_timeout_seconds, audit.max_audits_per_day) == (
        120,
        3600,
        20,
    )
    assert audit.escalate_paths == DEFAULT_ESCALATE_PATHS


def test_command_runner_takes_an_argv(tmp_path, policy_data):
    audit = load(
        tmp_path,
        policy_data,
        {"tiers": ["pr"], "runner": {"kind": "command", "command": ["claude", "-p", "audit"]}},
    ).audit
    assert audit.runner_command == ("claude", "-p", "audit")


@pytest.mark.parametrize(
    ("audit", "message"),
    [
        ({}, "missing keys"),
        ({"tiers": ["pr"], "surprise": 1}, "unknown keys"),
        ({"tiers": []}, "tiers"),
        ({"tiers": ["pr", "pr"]}, "tiers"),
        ({"tiers": ["nightly"]}, "tiers"),
        ({"tiers": ["pr"], "runner": {"kind": "daemon", "command": None}}, "runner kind"),
        ({"tiers": ["pr"], "runner": {"kind": "command", "command": "claude -p"}}, "argv"),
        ({"tiers": ["pr"], "runner": {"kind": "command", "command": []}}, "argv"),
        ({"tiers": ["pr"], "runner": {"kind": "command", "command": None}}, "argv"),
        ({"tiers": ["pr"], "runner": {"kind": "session", "command": ["x"]}}, "session runner"),
        ({"tiers": ["pr"], "full": {"schedule_days": 0, "max_age_days": 14}}, "schedule_days"),
        ({"tiers": ["pr"], "full": {"schedule_days": 14, "max_age_days": 7}}, "max_age_days"),
        ({"tiers": ["pr"], "full": {"schedule_days": 7.5, "max_age_days": 14}}, "schedule_days"),
        ({"tiers": ["pr"], "gates": {"pr": "severe"}}, "gate"),
        ({"tiers": ["pr"], "gates": {"full": "high"}}, "full audits never block"),
        ({"tiers": ["pr"], "escalate_paths": ["../etc/**"]}, "relative POSIX"),
        ({"tiers": ["pr"], "escalate_paths": "Dockerfile"}, "escalate_paths"),
        ({"tiers": ["pr"], "escalate_paths": [], "escalate_paths_add": ["x"]}, "either"),
        ({"tiers": ["pr"], "limits": {"max_audits_per_day": 0}}, "max_audits_per_day"),
        ({"tiers": ["pr"], "limits": {"audit_timeout_seconds": 5}}, "audit_timeout_seconds"),
        ({"tiers": ["pr"], "limits": {"debounce_seconds": -1}}, "debounce_seconds"),
        ("pr", "audit must be an object"),
    ],
)
def test_invalid_audit_policy_is_refused(tmp_path, policy_data, audit, message):
    with pytest.raises(CirraError, match=message):
        load(tmp_path, policy_data, audit)


@pytest.mark.parametrize(
    "event",
    [
        {"kind": "push"},
        {"kind": "change"},
        {"kind": "change", "changed_paths": "pyproject.toml"},
        {"kind": "change", "changed_paths": ["/etc/passwd"]},
        {"kind": "operator", "tier": "nightly"},
        {"kind": "release", "extra": True},
    ],
)
def test_malformed_events_are_refused(tmp_path, policy_data, event):
    config = load(tmp_path, policy_data, AUDIT)
    with pytest.raises(CirraError):
        select_tiers(config.audit, event, None, catalog_digest=CATALOG, now=NOW)


def test_malformed_history_is_refused(tmp_path, policy_data):
    config = load(tmp_path, policy_data, AUDIT)
    for latest in (
        {"completed_at": "yesterday", "catalog_digest": CATALOG},
        {"completed_at": NOW.isoformat(), "catalog_digest": "short"},
        {"completed_at": NOW.isoformat()},
        {"completed_at": NOW.isoformat(), "catalog_digest": CATALOG, "skill_version": 1},
        {"completed_at": NOW.isoformat(), "catalog_digest": CATALOG, "skill_version": ""},
    ):
        with pytest.raises(CirraError):
            select_tiers(
                config.audit, {"kind": "schedule"}, latest, catalog_digest=CATALOG, now=NOW
            )
    with pytest.raises(CirraError, match="timezone"):
        select_tiers(
            config.audit,
            {"kind": "schedule"},
            None,
            catalog_digest=CATALOG,
            now=NOW.replace(tzinfo=None),
        )


def test_future_completion_counts_as_fresh(tmp_path, policy_data):
    config = load(tmp_path, policy_data, AUDIT)
    assert (
        select_tiers(
            config.audit, {"kind": "schedule"}, full_audit(-1), catalog_digest=CATALOG, now=NOW
        )
        == ()
    )


def test_skill_version_change_selects_a_new_baseline(tmp_path, policy_data):
    config = load(tmp_path, policy_data, AUDIT)
    latest = {**full_audit(1), "skill_version": "1.0.0"}

    def select(version):
        return select_tiers(
            config.audit,
            {"kind": "schedule"},
            latest,
            catalog_digest=CATALOG,
            skill_version=version,
            now=NOW,
        )

    assert select("1.1.0") == (("full", "catalog"),)
    assert select("1.0.0") == ()
    # History without a skill version is compared by catalog alone.
    assert (
        select_tiers(
            config.audit,
            {"kind": "schedule"},
            full_audit(1),
            catalog_digest=CATALOG,
            skill_version="1.1.0",
            now=NOW,
        )
        == ()
    )
    with pytest.raises(CirraError, match="skill_version"):
        select(2)
