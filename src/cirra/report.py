"""Render local evidence. This module has no provider publishing authority."""

import html
import json
import re
import uuid
from pathlib import Path

from cirra.domain import digest, utc_now


def make_report(snapshot, ci, review, identity, run_id, config):
    gaps = list(snapshot.coverage_gaps)
    ci_complete = all(
        item["verified_revision"] and item["state"] in ("success", "failure")
        for item in ci["expected"]
    )
    complete = not gaps and ci_complete and review["outcome"] == "succeeded"
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "observed_at": utc_now(),
        "advisory": True,
        "status": "complete" if complete else "incomplete",
        "snapshot": snapshot.record(),
        "analysis_digest": digest(identity),
        "inputs": identity,
        "config_source": config.path,
        "ci": ci,
        "review": review,
        "coverage": {"complete": not gaps, "gaps": gaps},
    }
    report["report_digest"] = digest(report)
    return report


def plain(value):
    """Escape untrusted Markdown, HTML, mentions, links, and GitLab quick actions."""
    text = re.sub(r"([\\`*_{}\[\]()#+.!|~-])", r"\\\1", str(value))
    text = html.escape(text, quote=True)
    text = text.replace("@", "&#64;").replace("/", "&#47;")
    return text.replace("\r", " ").replace("\n", " ")


def render_markdown(report):
    snap, review, ci = report["snapshot"], report["review"], report["ci"]
    lines = [
        "# Cirra review",
        "",
        "Advisory report. Maintainers retain merge authority.",
        "",
        f"Status: **{plain(report['status'])}**. CI: **{plain(ci['outcome'])}**. "
        f"Reviewer: **{plain(review['outcome'])}**.",
        "",
        f"Change: {plain(snap['provider'])} / {plain(snap['repository_id'])} / "
        f"{plain(snap['change_id'])}",
        "",
        f"Head: `{snap['head_sha']}`  ",
        f"Diff base: `{snap['diff_base_sha']}`  ",
        f"Target: `{snap['target_sha']}`  ",
        f"Analysis: `{report['analysis_digest']}`  ",
        f"Report: `{report['report_digest']}`  ",
        f"Observed: {plain(report['observed_at'])}",
        "",
        "## CI evidence",
        "",
    ]
    if not ci["expected"]:
        lines.append("Review-only policy: no conventional CI checks are required.")
    for expected in ci["expected"]:
        evidence = expected["evidence"]
        relation = evidence["relationship"] if evidence else "no verified evidence"
        lines.append(
            f"- {plain(expected['name'])} (source {plain(expected['source_id'])}): "
            f"{plain(expected['state'])}; {plain(relation)}. {plain(expected['reason'])}."
        )
    lines.extend(["", "## Review findings", ""])
    if not review["findings"]:
        lines.append(
            "No findings returned."
            if review["outcome"] == "succeeded"
            else "Review unavailable; this is not a zero-finding review."
        )
    for finding in review["findings"]:
        lines.append(
            f"- **{plain(finding['severity'])}: {plain(finding['title'])}** — "
            f"{plain(finding['path'])}, {finding['side']} lines "
            f"{finding['start_line']}–{finding['end_line']}. {plain(finding['evidence'])}"
        )
    if review["notes"]:
        lines.extend(["", "## Unlocated notes", ""])
        lines.extend(f"- {plain(note)}" for note in review["notes"])
    lines.extend(["", "## Coverage and errors", ""])
    limitations = report["coverage"]["gaps"] + review["errors"]
    lines.extend(f"- {plain(item)}" for item in limitations)
    if not limitations:
        lines.append("All supplied textual diff hunks were available; no reviewer errors recorded.")
    lines.extend(
        [
            "",
            f"Run: `{report['run_id']}`; attempt {review['attempt']}. "
            "The offline reviewer returns fixture data, not an AI quality assessment.",
            "",
        ]
    )
    return "\n".join(lines)


def export_report(report, state_dir):
    directory = Path(state_dir).resolve() / "reports" / report["run_id"]
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for extension, content in (
        ("json", json.dumps(report, indent=2, ensure_ascii=True) + "\n"),
        ("md", render_markdown(report)),
    ):
        destination = directory / f"{report['report_digest']}.{extension}"
        temporary = directory / f".{uuid.uuid4().hex}.tmp"
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(destination)
        paths["json_path" if extension == "json" else "markdown_path"] = str(destination)
    return paths
