"""Stage 2 of the production-audit tiers plan: prepare, import, identity and baselines."""

import json
import sqlite3

import pytest
from audit_fixtures import Repository, audit_policy, build_report, lines, write_report

from cirra import audits, store
from cirra.cli import main
from cirra.domain import CirraError
from cirra.store import Store


@pytest.fixture
def setup(tmp_path, policy_data):
    repo = Repository(tmp_path / "repo")
    first = repo.commit({"app.py": lines(20), "deploy/app.service": lines(5)})
    second = repo.commit({"app.py": lines(10) + lines(12, "changed")})
    config = audit_policy(tmp_path, policy_data)
    repository = audits.select_repository(config)
    state = Store(tmp_path / "state")
    return repo, first, second, config, repository, state


def begin(config, repository, state, tier, head, base=None):
    identity = audits.audit_identity(config.audit, tier, repository, head, base)
    return audits.begin(state, identity)


def test_full_audit_imports_once_and_reimport_is_a_no_op(tmp_path, setup):
    repo, first, _, config, repository, state = setup
    attempt, reused = begin(config, repository, state, "full", first)
    assert not reused and attempt["state"] == "prepared"
    path = write_report(
        tmp_path / "out", build_report("full", first, gaps=[("C011", "app.py", 1, 2, "high")])
    )
    result, reused = audits.import_report(state, attempt["attempt_id"], path, repo.path)
    assert result["state"] == "complete" and not reused
    assert result["comparison"] == {
        "baseline_attempt": None,
        "new": ["C011:app.py"],
        "open": [],
        "resolved": [],
    }
    again, reused = audits.import_report(state, attempt["attempt_id"], path, repo.path)
    assert reused and again == result
    with sqlite3.connect(state.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audits").fetchone()[0] == 1
    # An identical identity is never run twice: preparing it again reuses the result.
    reused_attempt, reused = begin(config, repository, state, "full", first)
    assert reused and reused_attempt["attempt_id"] == attempt["attempt_id"]


def test_a_different_report_for_a_complete_attempt_is_refused(tmp_path, setup):
    repo, first, _, config, repository, state = setup
    attempt, _ = begin(config, repository, state, "full", first)
    audits.import_report(
        state,
        attempt["attempt_id"],
        write_report(tmp_path / "a", build_report("full", first)),
        repo.path,
    )
    other = build_report("full", first, gaps=[("C011", "app.py", 1, 1, "low")])
    with pytest.raises(CirraError, match="different report"):
        audits.import_report(
            state, attempt["attempt_id"], write_report(tmp_path / "b", other), repo.path
        )


def test_invalid_report_is_a_failed_attempt_with_validator_output(tmp_path, setup):
    repo, first, _, config, repository, state = setup
    attempt, _ = begin(config, repository, state, "full", first)
    report = build_report("full", first)
    report["coverage"] = report["coverage"][1:]  # drops a catalog ID
    result, _ = audits.import_report(
        state, attempt["attempt_id"], write_report(tmp_path / "out", report), repo.path
    )
    assert result["state"] == "failed"
    assert any(
        "full tier must cover every catalog id" in line for line in result["validator_output"]
    )
    assert audits.latest_full(state, audits.repository_key(repository)) is None
    with pytest.raises(CirraError, match="attempt is failed"):
        audits.import_report(
            state, attempt["attempt_id"], write_report(tmp_path / "again", report), repo.path
        )
    retry, reused = begin(config, repository, state, "full", first)
    assert not reused and retry["number"] == 2


def test_out_of_range_evidence_fails_validation_against_the_checkout(tmp_path, setup):
    repo, first, _, config, repository, state = setup
    attempt, _ = begin(config, repository, state, "full", first)
    report = build_report("full", first, gaps=[("C011", "app.py", 30, 31, "high")])
    result, _ = audits.import_report(
        state, attempt["attempt_id"], write_report(tmp_path / "out", report), repo.path
    )
    assert result["state"] == "failed"
    assert any("app.py has 20 lines" in line for line in result["validator_output"])


def test_catalog_mismatch_names_both_digests(tmp_path, setup):
    repo, first, _, config, repository, state = setup
    attempt, _ = begin(config, repository, state, "full", first)
    report = {**build_report("full", first), "catalog_digest": "0" * 64}
    result, _ = audits.import_report(
        state, attempt["attempt_id"], write_report(tmp_path / "out", report), repo.path
    )
    assert result["state"] == "failed"
    [line] = [line for line in result["validator_output"] if "catalog_digest" in line]
    assert "0" * 64 in line and audits.vendored_identity()["catalog_digest"] in line


def test_report_for_another_revision_is_refused_without_spending_the_attempt(tmp_path, setup):
    repo, first, second, config, repository, state = setup
    attempt, _ = begin(config, repository, state, "full", first)
    path = write_report(tmp_path / "out", build_report("full", second))
    with pytest.raises(CirraError, match="repository.head"):
        audits.import_report(state, attempt["attempt_id"], path, repo.path)
    wrong_tier = write_report(tmp_path / "rel", build_report("release", first))
    with pytest.raises(CirraError, match="does not match the full attempt"):
        audits.import_report(state, attempt["attempt_id"], wrong_tier, repo.path)
    assert audits.get(state, attempt["attempt_id"])["state"] == "prepared"


def test_second_full_audit_marks_findings_new_open_and_resolved(tmp_path, setup):
    repo, first, second, config, repository, state = setup
    attempt, _ = begin(config, repository, state, "full", first)
    baseline = build_report(
        "full",
        first,
        gaps=[("C011", "app.py", 1, 2, "high"), ("C058", "deploy/app.service", 1, 1, "medium")],
    )
    audits.import_report(
        state, attempt["attempt_id"], write_report(tmp_path / "one", baseline), repo.path
    )
    attempt, _ = begin(config, repository, state, "full", second)
    later = build_report(
        "full", second, gaps=[("C011", "app.py", 3, 4, "high"), ("C020", "app.py", 5, 5, "low")]
    )
    result, _ = audits.import_report(
        state, attempt["attempt_id"], write_report(tmp_path / "two", later), repo.path
    )
    comparison = result["comparison"]
    assert comparison["new"] == ["C020:app.py"]
    assert comparison["open"] == ["C011:app.py"]
    assert comparison["resolved"] == ["C058:deploy/app.service"]
    newest = audits.latest_full(state, audits.repository_key(repository))
    assert newest["attempt_id"] == attempt["attempt_id"]


def test_pr_audit_is_compared_with_the_full_baseline_and_gets_its_profile(tmp_path, setup):
    repo, first, second, config, repository, state = setup
    full, _ = begin(config, repository, state, "full", first)
    audits.import_report(
        state,
        full["attempt_id"],
        write_report(
            tmp_path / "full",
            build_report(
                "full",
                first,
                gaps=[
                    ("C011", "app.py", 11, 12, "high"),
                    ("C058", "deploy/app.service", 1, 1, "medium"),
                ],
            ),
        ),
        repo.path,
    )
    attempt, _ = begin(config, repository, state, "pr", second, first)
    packet = audits.make_packet(state, attempt, repo.path)
    assert packet["profile"]["summary"] == "Fixture service"
    assert packet["repository"]["base"] == first and packet["tier"] == "pr"
    report = build_report(
        "pr",
        second,
        first,
        gaps=[("C011", "app.py", 11, 12, "high"), ("C012", "app.py", 13, 13, "critical")],
    )
    result, _ = audits.import_report(
        state, attempt["attempt_id"], write_report(tmp_path / "pr", report), repo.path
    )
    assert result["state"] == "complete"
    assert result["comparison"]["new"] == ["C012:app.py"]
    assert result["comparison"]["open"] == ["C011:app.py"]
    assert result["comparison"]["resolved"] == []


def test_pr_findings_outside_the_diff_fail(tmp_path, setup):
    repo, first, second, config, repository, state = setup
    attempt, _ = begin(config, repository, state, "pr", second, first)
    report = build_report("pr", second, first, gaps=[("C011", "app.py", 1, 2, "high")])
    result, _ = audits.import_report(
        state, attempt["attempt_id"], write_report(tmp_path / "pr", report), repo.path
    )
    assert result["state"] == "failed"
    assert any("outside the diff" in line for line in result["validator_output"])


def test_identity_covers_tier_revisions_skill_catalog_and_policy(tmp_path, setup, policy_data):
    _, first, second, config, repository, _ = setup
    identity = audits.audit_identity(config.audit, "pr", repository, second, first)
    assert identity["skill_version"] == "1.1.0"
    assert identity["catalog_digest"] == audits.vendored_identity()["catalog_digest"]
    other = audit_policy(
        tmp_path, policy_data, {"tiers": ["pr", "full"], "gates": {"pr": "high"}}, "other.yaml"
    )
    assert identity != audits.audit_identity(other.audit, "pr", repository, second, first)
    with pytest.raises(CirraError, match="diff base"):
        audits.audit_identity(config.audit, "full", repository, second, first)
    with pytest.raises(CirraError, match="not enabled"):
        audits.audit_identity(other.audit, "release", repository, second)
    with pytest.raises(CirraError, match="no audit block"):
        audits.audit_identity(None, "full", repository, second)


def test_version_one_state_gains_the_audit_table(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "SCHEMA_VERSION", 1)
    Store(tmp_path)
    monkeypatch.undo()
    Store(tmp_path)
    with sqlite3.connect(tmp_path / "cirra.sqlite3") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
        assert connection.execute("SELECT COUNT(*) FROM audits").fetchone()[0] == 0


def test_cli_prepare_and_import(tmp_path, setup, capsys):
    repo, first, _, config, _, _ = setup
    state = tmp_path / "cli-state"
    common = ["--state-dir", str(state)]
    assert (
        main(
            [
                "prepare-audit",
                "--config",
                config.path,
                "--tier",
                "full",
                "--head",
                first,
                "--checkout",
                str(repo.path),
                *common,
            ]
        )
        == 0
    )
    prepared = json.loads(capsys.readouterr().out)
    packet = json.loads(open(prepared["packet_path"], encoding="utf-8").read())
    assert packet["attempt_id"] == prepared["attempt_id"]
    assert packet["skill"]["catalog_digest"] == audits.vendored_identity()["catalog_digest"]
    path = write_report(tmp_path / "out", build_report("full", first))
    arguments = ["import-audit", "--attempt", prepared["attempt_id"], "--report", str(path)]
    assert main([*arguments, "--checkout", str(repo.path), *common]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "complete"
    assert (
        main(
            [
                "prepare-audit",
                "--config",
                config.path,
                "--tier",
                "full",
                "--head",
                "f" * 40,
                "--checkout",
                str(repo.path),
                *common,
            ]
        )
        == 2
    )
    assert "does not contain the head commit" in capsys.readouterr().err


def test_cancellation_during_validation_wins_over_the_late_import(tmp_path, setup, monkeypatch):
    repo, first, _, config, repository, state = setup
    attempt, _ = begin(config, repository, state, "full", first)
    real = audits.validate_report.validate

    def cancel_then_validate(report, checkout):
        with state.transaction() as connection:
            connection.execute(
                "UPDATE audits SET state='cancelled' WHERE attempt_id=?", (attempt["attempt_id"],)
            )
        return real(report, checkout)

    monkeypatch.setattr(audits.validate_report, "validate", cancel_then_validate)
    path = write_report(tmp_path / "out", build_report("full", first))
    with pytest.raises(CirraError, match="cancelled or superseded"):
        audits.import_report(state, attempt["attempt_id"], path, repo.path)
    assert audits.get(state, attempt["attempt_id"])["report_json"] is None
