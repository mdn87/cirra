"""Validate a production-audit JSON report against references/report-format.md.

Usage (Python 3.9+; `python3` on Linux/macOS, `py -3` on Windows):
    python validate_report.py <report.json> [--repo <path>]
    python validate_report.py --identity

Exits 0 when valid, 1 with one problem per line otherwise. With --repo, also
checks that every cited path exists and every line range fits the file. When
--repo is a git work tree, files are read at repository.head rather than from
the working tree; for the pr tier, old-side findings are read at the merge base
with repository.base and every finding must overlap the diff on its side.
--identity prints the skill_version and catalog_digest a report must carry.
Standard library only, so it runs from any agent host.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import sys

CONCEPTS = Path(__file__).resolve().parent.parent / "references" / "concepts.md"
# Bump when the report contract or procedure changes in a way consumers notice.
SKILL_VERSION = "1.1.0"
LIKELIHOOD_RANK = {"unlikely": 0, "possible": 1, "likely": 2}
EFFORT_RANK = {"S": 0, "M": 1, "L": 2}
FILE_NAME_RE = re.compile(r"production-audit-(pr|full|release)-(\d{8}-\d{6})\.json")
TIERS = {"pr", "full", "release"}
STATUSES = {"implemented", "incomplete", "missing", "needs_investigation", "not_applicable"}
PRIORITIES = {"critical", "high", "medium", "low"}
LIKELIHOODS = {"likely", "possible", "unlikely"}
EFFORTS = {"S", "M", "L"}
FINDING_FIELDS = {"path", "side", "start_line", "end_line", "severity", "title", "evidence"}
SEVERITIES = PRIORITIES
CONCEPT_ID = r"(?:C\d{3}|X\d{2})"
TITLE_RE = re.compile(rf"\[({CONCEPT_ID}(?:, ?{CONCEPT_ID})*)\] \S")
RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
UNRATED = {"implemented", "not_applicable"}
PROFILE_LISTS = ("languages", "runtime", "datastores", "external_services", "deployment", "exposure")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
GIT_TIMEOUT = 30


class GitError(Exception):
    pass


def catalog_tiers():
    """Map each catalog ID to its tier letters, read from the Tiers column."""
    rows = re.findall(r"^\| (C\d{3}|X\d{2}) \|[^|]*\| ([PFR ]+) \|", CONCEPTS.read_text(encoding="utf-8"), re.M)
    return {cid: set(tiers.split()) for cid, tiers in rows}


def catalog_ids():
    return set(catalog_tiers())


def catalog_digest():
    """sha256 of the catalog with LF line endings, so CRLF checkouts agree."""
    return hashlib.sha256(CONCEPTS.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def identity():
    return {"skill_version": SKILL_VERSION, "catalog_digest": catalog_digest()}


def _git(repo, *args):
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "-c", "core.quotePath=false", *args],
            capture_output=True, timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitError(str(error)) from None
    if result.returncode:
        raise GitError(result.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed")
    return result.stdout


def _diff_path(text, prefix):
    text = text.rstrip("\t")
    if text == "/dev/null":
        return None
    return text[len(prefix):] if text.startswith(prefix) else text


def _diff_hunks(repo, old, new):
    """Changed line ranges per side and path in `git diff old new`."""
    hunks = {"old": {}, "new": {}}
    old_path = new_path = None
    in_header = False
    output = _git(repo, "diff", "--no-color", "--no-ext-diff", "--no-renames", "--unified=0",
                  "--src-prefix=a/", "--dst-prefix=b/", old, new)
    for line in output.decode("utf-8", "replace").splitlines():
        if line.startswith("diff --git "):
            in_header, old_path, new_path = True, None, None
        elif in_header and line.startswith("--- "):
            old_path = _diff_path(line[4:], "a/")
        elif in_header and line.startswith("+++ "):
            new_path = _diff_path(line[4:], "b/")
        elif match := HUNK_RE.match(line):
            in_header = False
            for side, path, start, count in (("old", old_path, match[1], match[2]), ("new", new_path, match[3], match[4])):
                count = 1 if count is None else int(count)
                if path and count:
                    hunks[side].setdefault(path, []).append((int(start), int(start) + count - 1))
    return hunks


def _path_problem(path):
    if not isinstance(path, str) or not path:
        return "path must be a non-empty string"
    if PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute() or PureWindowsPath(path).drive:
        return f"path {path!r} must be relative to the repository root"
    if ".." in re.split(r"[\\/]", path):
        return f"path {path!r} must stay inside the repository"
    return None


def _worktree_lines(repo):
    root = repo.resolve()

    def lines(path):
        target = (repo / path).resolve()
        if not target.is_relative_to(root):
            return None, f"{path} resolves outside the repository"
        if not target.is_file():
            return None, f"{path} does not exist in the repository"
        return len(target.read_text(encoding="utf-8", errors="replace").splitlines()), ""

    return lines


def _revision_lines(repo, revision, label):
    def lines(path):
        try:
            content = _git(repo, "show", f"{revision}:{path}")
        except GitError:
            return None, f"{path} does not exist at {label}"
        return len(content.decode("utf-8", "replace").splitlines()), f" at {label}"

    return lines


def _head_reader(report, repo):
    """Line reader at repository.head when --repo is a git work tree, else the working tree."""
    try:
        _git(repo, "rev-parse", "--is-inside-work-tree")
    except GitError:
        return _worktree_lines(repo), None
    head = report["repository"]["head"]
    try:
        commit = _git(repo, "rev-parse", "--verify", f"{head}^{{commit}}").decode().strip()
    except GitError:
        return None, f"repository.head {head!r} does not resolve in --repo"
    return _revision_lines(repo, commit, head), None


def _range_problems(where, item, lines_of):
    """Shape, containment, and (when lines_of is given) existence of a cited range."""
    path_problem = _path_problem(item.get("path"))
    if path_problem:
        return [f"{where}: {path_problem}"]
    start, end = item.get("start_line"), item.get("end_line")
    if not (type(start) is int and type(end) is int and 1 <= start <= end):
        return [f"{where}: start_line/end_line must be integers with 1 <= start <= end"]
    if lines_of is None:
        return []
    count, detail = lines_of(item["path"])
    if count is None:
        return [f"{where}: {detail}"]
    if end > count:
        return [f"{where}: {item['path']} has {count} lines{detail}, range ends at {end}"]
    return []


def _metadata_problems(report, tier):
    problems = []
    repository = report.get("repository")
    if not isinstance(repository, dict):
        problems.append("repository must be an object")
    else:
        for key in ("path", "head"):
            if not isinstance(repository.get(key), str) or not repository[key].strip():
                problems.append(f"repository.{key} must be a non-empty string")
        base = repository.get("base")
        if tier == "pr" and (not isinstance(base, str) or not base.strip()):
            problems.append("repository.base is required for the pr tier")
        elif base is not None and not isinstance(base, str):
            problems.append("repository.base must be a string or null")
    generated = report.get("generated_at")
    try:
        datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        problems.append("generated_at must be an ISO-8601 timestamp")
    profile = report.get("profile")
    if not isinstance(profile, dict):
        problems.append("profile must be an object")
    else:
        for key in ("summary", "tests"):
            if not isinstance(profile.get(key), str) or not profile[key].strip():
                problems.append(f"profile.{key} must be a non-empty string")
        for key in PROFILE_LISTS:
            value = profile.get(key)
            if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
                problems.append(f"profile.{key} must be a list of strings")
    return problems


def _pr_context(report, repo):
    """(revision line readers per side, diff hunks per side) for a pr-tier report."""
    repository = report["repository"]
    head = _git(repo, "rev-parse", "--verify", f"{repository['head']}^{{commit}}").decode().strip()
    base = _git(repo, "rev-parse", "--verify", f"{repository['base']}^{{commit}}").decode().strip()
    merge_base = _git(repo, "merge-base", base, head).decode().strip()
    readers = {
        "new": _revision_lines(repo, head, repository["head"]),
        "old": _revision_lines(repo, merge_base, "the merge base"),
    }
    return readers, _diff_hunks(repo, merge_base, head)


def validate(report, repo=None):
    if not isinstance(report, dict):
        return ["report must be an object"]
    problems = []
    tiers = catalog_tiers()
    known = set(tiers)
    if report.get("schema_version") != 1:
        problems.append("schema_version must be 1")
    if report.get("skill") != "production-audit":
        problems.append("skill must be 'production-audit'")
    if not isinstance(report.get("skill_version"), str) or not report["skill_version"].strip():
        problems.append("skill_version must be a non-empty string")
    expected_catalog = catalog_digest()
    if report.get("catalog_digest") != expected_catalog:
        problems.append(
            f"catalog_digest {report.get('catalog_digest')!r} does not match this validator's "
            f"catalog {expected_catalog}; regenerate the report with the current skill"
        )
    tier = report.get("tier")
    if tier not in TIERS:
        problems.append(f"tier must be one of {sorted(TIERS)}")
    problems += _metadata_problems(report, tier)

    coverage = report.get("coverage")
    if not isinstance(coverage, list):
        return problems + ["coverage must be a list"]
    source = None
    if repo is not None:
        if any(p.startswith("repository.") for p in problems):
            source = _worktree_lines(repo)
        else:
            source, head_problem = _head_reader(report, repo)
            if head_problem:
                problems.append(head_problem)
    seen = {}
    for index, row in enumerate(coverage):
        where = f"coverage[{index}]"
        if not isinstance(row, dict):
            problems.append(f"{where}: must be an object")
            continue
        cid = row.get("id")
        if cid not in known:
            problems.append(f"{where}: unknown concept id {cid!r}")
            continue
        if cid in seen:
            problems.append(f"{where}: duplicate concept id {cid}")
        seen[cid] = row
        where = f"coverage {cid}"
        status = row.get("status")
        if status not in STATUSES:
            problems.append(f"{where}: status must be one of {sorted(STATUSES)}")
            continue
        if not isinstance(row.get("rationale"), str) or not row["rationale"].strip():
            problems.append(f"{where}: rationale is required")
        for field, allowed in (("priority", PRIORITIES), ("likelihood", LIKELIHOODS), ("effort", EFFORTS)):
            value = row.get(field)
            if status in UNRATED and value is not None:
                problems.append(f"{where}: {field} must be null when status is {status}")
            if status not in UNRATED and value not in allowed:
                problems.append(f"{where}: {field} must be one of {sorted(allowed)}")
        evidence = row.get("evidence")
        if not isinstance(evidence, list):
            problems.append(f"{where}: evidence must be a list")
            continue
        if status in {"implemented", "incomplete"} and not evidence:
            problems.append(f"{where}: {status} needs at least one evidence range")
        for number, item in enumerate(evidence):
            if not isinstance(item, dict):
                problems.append(f"{where} evidence[{number}]: must be an object")
                continue
            problems += _range_problems(f"{where} evidence[{number}]", item, source)
    required = {"full": known, "release": {cid for cid, letters in tiers.items() if "R" in letters}}.get(tier)
    if required is not None and not required <= set(seen):
        label = "every catalog id" if tier == "full" else "every R-tagged catalog id"
        problems.append(f"{tier} tier must cover {label}; missing {', '.join(sorted(required - set(seen)))}")

    findings = report.get("findings")
    if not isinstance(findings, list):
        return problems + ["findings must be a list"]
    readers = hunks = None
    pr_checks = tier == "pr" and repo is not None and not any(p.startswith("repository.") for p in problems)
    if pr_checks:
        try:
            readers, hunks = _pr_context(report, repo)
        except GitError as error:
            problems.append(f"cannot read the pr diff from --repo: {error}")
    cited = set()
    order = []
    for index, finding in enumerate(findings):
        where = f"findings[{index}]"
        if not isinstance(finding, dict):
            problems.append(f"{where}: must be an object")
            continue
        if set(finding) != FINDING_FIELDS:
            problems.append(f"{where}: fields must be exactly {sorted(FINDING_FIELDS)}")
            continue
        side = finding["side"]
        if side not in {"old", "new"}:
            problems.append(f"{where}: side must be 'old' or 'new'")
            continue
        if side == "old" and tier != "pr":
            problems.append(f"{where}: side 'old' is only valid in the pr tier")
        if finding["severity"] not in SEVERITIES:
            problems.append(f"{where}: severity must be one of {sorted(SEVERITIES)}")
        match = TITLE_RE.match(finding["title"] if isinstance(finding["title"], str) else "")
        if not match:
            problems.append(f"{where}: title must start with '[<concept id>[, <concept id>...]] '")
        else:
            priorities = []
            for cid in (part.strip() for part in match.group(1).split(",")):
                if cid not in seen:
                    problems.append(f"{where}: {cid} has no coverage row")
                    continue
                if seen[cid].get("status") in UNRATED:
                    problems.append(f"{where}: {cid} is {seen[cid]['status']}, so it cannot carry a finding")
                if cid in cited:
                    problems.append(f"{where}: {cid} already appears in another finding")
                cited.add(cid)
                if seen[cid].get("priority") in RANK:
                    priorities.append(seen[cid]["priority"])
            if priorities:
                expected = max(priorities, key=RANK.get)
                if finding["severity"] != expected:
                    problems.append(f"{where}: severity {finding['severity']} differs from coverage priority {expected}")
            rows = [seen[cid] for cid in (part.strip() for part in match.group(1).split(",")) if cid in seen]
            likelihoods = [LIKELIHOOD_RANK[r["likelihood"]] for r in rows if r.get("likelihood") in LIKELIHOOD_RANK]
            efforts = [EFFORT_RANK[r["effort"]] for r in rows if r.get("effort") in EFFORT_RANK]
            if finding["severity"] in RANK and likelihoods and efforts:
                # Severity, then the highest likelihood, then the largest effort among the rows.
                order.append((-RANK[finding["severity"]], -max(likelihoods), max(efforts)))
        if not isinstance(finding["evidence"], str) or not finding["evidence"].strip():
            problems.append(f"{where}: evidence is required")
        if tier == "pr":
            if readers is None:
                problems += _range_problems(where, finding, None)
                continue
            range_problems = _range_problems(where, finding, readers[side])
            problems += range_problems
            if not range_problems:
                path = finding["path"].replace("\\", "/")
                start, end = finding["start_line"], finding["end_line"]
                if not any(low <= end and start <= high for low, high in hunks[side].get(path, [])):
                    problems.append(f"{where}: {path}:{start}-{end} on the {side} side is outside the diff")
        else:
            problems += _range_problems(where, finding, source)
    if order != sorted(order):
        problems.append("findings must be sorted by priority, then likelihood, then effort (smallest first)")
    for cid, row in seen.items():
        needs = row.get("status") in {"missing", "incomplete"} or (
            row.get("status") == "needs_investigation" and row.get("priority") in {"critical", "high"}
        )
        if needs and cid not in cited:
            problems.append(f"coverage {cid}: {row['status']} row has no finding")
    if not isinstance(report.get("notes", []), list):
        problems.append("notes must be a list")
    return problems


def file_problems(path, report):
    """Report file naming, its UTC timestamp, and the companion Markdown report."""
    match = FILE_NAME_RE.fullmatch(path.name)
    if not match:
        return [f"report file must be named production-audit-<tier>-<YYYYMMDD-HHMMSS>.json, not {path.name}"]
    problems = []
    if isinstance(report, dict) and match.group(1) != report.get("tier"):
        problems.append(f"file name tier {match.group(1)} differs from report tier {report.get('tier')}")
    try:
        stamp = datetime.strptime(match.group(2), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        generated = datetime.fromisoformat(report["generated_at"].replace("Z", "+00:00"))
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        if abs((generated - stamp).total_seconds()) > 300:
            problems.append("file name timestamp differs from generated_at by more than 5 minutes; both are UTC")
    except (KeyError, TypeError, AttributeError, ValueError):
        pass  # validate() reports a malformed generated_at
    if not path.with_suffix(".md").is_file():
        problems.append(f"companion report {path.with_suffix('.md').name} is missing")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", nargs="?")
    parser.add_argument("--repo", help="repository root; enables path, line-range, and pr diff checks")
    parser.add_argument("--identity", action="store_true", help="print the identity a report must carry")
    args = parser.parse_args(argv)
    if args.identity:
        print(json.dumps(identity()))
        return 0
    if not args.report:
        parser.error("a report path is required unless --identity is given")
    try:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"cannot read report: {error}")
        return 1
    problems = file_problems(Path(args.report), report) + validate(report, Path(args.repo) if args.repo else None)
    for problem in problems:
        print(problem)
    if not problems:
        rows = report["coverage"]
        counts = {status: sum(1 for row in rows if row["status"] == status) for status in sorted(STATUSES)}
        print(f"valid: tier={report['tier']} rows={len(rows)} findings={len(report['findings'])} {counts}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
