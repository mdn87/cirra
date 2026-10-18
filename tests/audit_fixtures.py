"""Local Git repositories and validator-clean production-audit reports for tests."""

import json
import subprocess
from datetime import UTC, datetime

import yaml

from cirra.config import load_config
from cirra.vendor.production_audit.scripts import validate_report

RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class Repository:
    def __init__(self, path):
        self.path = path
        path.mkdir(parents=True)
        self.git("init", "--initial-branch=main")

    def git(self, *args):
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=Cirra fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "core.autocrlf=false",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "gc.autoDetach=false",
                "-c",
                "maintenance.autoDetach=false",
                *args,
            ],
            cwd=self.path,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()

    def commit(self, files, message="fixture"):
        for name, content in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            self.git("add", name)
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")


def lines(count, label="line"):
    return "".join(f"{label} {number}\n" for number in range(1, count + 1))


def audit_policy(tmp_path, policy_data, audit=None, name="audit-policy.yaml"):
    policy = dict(policy_data)
    policy["audit"] = audit or {"tiers": ["pr", "release", "full"]}
    path = tmp_path / name
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    return load_config(path)


def build_report(tier, head, base=None, gaps=(), repo_path="repo"):
    """A report the vendored validator accepts.

    `gaps` are (concept_id, path, start_line, end_line, priority) tuples; each
    becomes a `missing` coverage row and its finding.
    """
    tiers = validate_report.catalog_tiers()
    if tier == "full":
        ids = set(tiers)
    elif tier == "release":
        ids = {cid for cid, letters in tiers.items() if "R" in letters}
    else:
        ids = set()
    ids |= {gap[0] for gap in gaps}
    rows = {
        cid: {
            "id": cid,
            "concept": cid,
            "status": "not_applicable",
            "priority": None,
            "likelihood": None,
            "effort": None,
            "rationale": "No signal in this fixture.",
            "evidence": [],
        }
        for cid in ids
    }
    findings = []
    for cid, path, start, end, priority in gaps:
        rows[cid].update(status="missing", priority=priority, likelihood="likely", effort="S")
        findings.append(
            {
                "path": path,
                "side": "new",
                "start_line": start,
                "end_line": end,
                "severity": priority,
                "title": f"[{cid}] Fixture gap",
                "evidence": "Fixture evidence.",
            }
        )
    findings.sort(key=lambda item: -RANK[item["severity"]])
    return {
        "schema_version": 1,
        "skill": "production-audit",
        **validate_report.identity(),
        "tier": tier,
        "repository": {"path": str(repo_path), "head": head, "base": base},
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "profile": {
            "summary": "Fixture service",
            "languages": ["python"],
            "runtime": ["cli"],
            "datastores": [],
            "external_services": [],
            "deployment": [],
            "exposure": [],
            "tests": "pytest",
        },
        "coverage": [rows[cid] for cid in sorted(rows)],
        "findings": findings,
        "notes": [],
    }


def domain_rows(slug, evidence_paths=(), status="implemented"):
    """Coverage rows for every concept of one catalog domain, all citing `evidence_paths`."""
    from cirra.audits import catalog_domains

    domain = next(item for item in catalog_domains() if item["slug"] == slug)
    return [
        {
            "id": cid,
            "status": status,
            "evidence": [{"path": path, "start_line": 1, "end_line": 1} for path in evidence_paths],
        }
        for cid in domain["ids"]
    ]


def write_report(directory, report):
    """Write the JSON and companion Markdown with the skill's file naming."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(report["generated_at"].replace("Z", "+00:00"))
    stem = f"production-audit-{report['tier']}-{stamp:%Y%m%d-%H%M%S}"
    path = directory / f"{stem}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    path.with_suffix(".md").write_text("# Production audit\n", encoding="utf-8")
    return path
