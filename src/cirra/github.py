"""Read GitHub snapshots and native CI associations without executing changed code."""

import json
import re
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cirra.domain import CirraError, digest, number, path_name, sha, string, utc_now
from cirra.fixture import FixtureProvider, validate_check
from cirra.reviewer import unique_object

API_VERSION = "2026-03-10"
OID = r"(?:[a-f0-9]{40}|[a-f0-9]{64})"
ROUTE = re.compile(
    rf"(?:pulls(?:/[1-9][0-9]*)?|branches/(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{{2}})+"
    rf"|compare/{OID}\.\.\.{OID}|commits/{OID}/(?:check-runs|statuses))?"
)


class GitHubError(CirraError):
    """A safe transport error containing neither server payloads nor credentials."""


class GitHubRateLimited(GitHubError):
    """GitHub refused the read for quota reasons; `retry_at` is when it resets (epoch seconds)."""

    def __init__(self, message, retry_at):
        super().__init__(message)
        self.retry_at = retry_at


def rate_limit_reset(headers, now=None):
    """Epoch seconds when GitHub says a limited read may be retried (default: one minute)."""
    now = time.time() if now is None else now
    try:
        retry_after = headers.get("Retry-After")
        if retry_after is not None:
            return now + min(max(int(retry_after), 1), 3600)
        reset = headers.get("X-RateLimit-Reset")
        if reset is not None:
            return min(max(int(reset), now + 1), now + 3600)
    except (TypeError, ValueError):
        pass
    return now + 60


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def repository_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", value):
        raise CirraError("GitHub repository must be owner/name, not a URL or path")
    if value.split("/")[1] in (".", ".."):
        raise CirraError("invalid GitHub repository name")
    return value


class GitHubClient:
    def __init__(
        self, repository, *, token=None, timeout=60, max_bytes=8 * 1024 * 1024, opener=None
    ):
        self.repository = repository_name(repository)
        number(timeout, "collection timeout", 1, 600)
        number(max_bytes, "API response byte limit", 1, 16 * 1024 * 1024, integral=True)
        self.deadline = time.monotonic() + timeout
        self.max_bytes = max_bytes
        self.opener = opener if opener is not None else build_opener(NoRedirect())
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "cirra-read-only-collector",
        }
        if token is not None:
            string(token, "GitHub reader token")
            self.headers["Authorization"] = f"Bearer {token}"

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise GitHubError("GitHub collection deadline exceeded; retry collection")
        return remaining

    def get(self, route="", params=None):
        if not isinstance(route, str) or not ROUTE.fullmatch(route):
            raise CirraError("unsupported GitHub read endpoint")
        url = f"https://api.github.com/repos/{self.repository}"
        if route:
            url += "/" + route
        if params:
            url += "?" + urlencode(params)
        request = Request(url, headers=self.headers, method="GET")
        for attempt in range(3):
            try:
                with self.opener.open(request, timeout=min(10, self.remaining())) as response:
                    chunks, count = [], 0
                    read = getattr(response, "read1", response.read)
                    while True:
                        self.remaining()
                        chunk = read(min(65536, self.max_bytes + 1 - count))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        count += len(chunk)
                        if count > self.max_bytes:
                            raise GitHubError("GitHub API response byte limit exceeded")
                    try:
                        data = json.loads(b"".join(chunks), object_pairs_hook=unique_object)
                    except (ValueError, UnicodeError, RecursionError, CirraError):
                        raise GitHubError("GitHub returned invalid JSON") from None
                    # Only use the presence of another page. Never follow a server-supplied URL.
                    more = bool(re.search(r'rel="next"', response.headers.get("Link", "")))
                    return data, more
            except HTTPError as exc:
                status = exc.code
                limited = status == 429 or exc.headers.get("X-RateLimit-Remaining") == "0"
                retry_at = rate_limit_reset(exc.headers) if limited else None
                exc.close()
                if 300 <= status < 400:
                    raise GitHubError(
                        "GitHub redirect refused; use the canonical repository name"
                    ) from None
                if limited:
                    raise GitHubRateLimited(
                        f"GitHub rate limit reached (HTTP {status}); retry later", retry_at
                    ) from None
                if status in (401, 403):
                    raise GitHubError(
                        f"GitHub access denied (HTTP {status}); check reader permissions"
                    ) from None
                if status not in (500, 502, 503, 504) or attempt == 2:
                    raise GitHubError(f"GitHub request failed (HTTP {status})") from None
            except (URLError, OSError):
                if attempt == 2:
                    raise GitHubError(
                        "GitHub network request failed after bounded retries"
                    ) from None
            time.sleep(min(0.25 * (attempt + 1), self.remaining()))
        raise GitHubError("GitHub request did not complete")


def record(value):
    if not isinstance(value, dict):
        raise CirraError("GitHub returned a malformed record")
    return value


def native_id(value):
    return str(number(value, "GitHub native ID", 1, 2**63 - 1, integral=True))


def revisions(pr, repository_id, change):
    record(pr)
    if type(pr.get("number")) is not int or pr["number"] != change:
        raise CirraError("GitHub response does not match the requested pull request")
    head, base = record(pr.get("head")), record(pr.get("base"))
    if native_id(record(base.get("repo")).get("id")) != repository_id:
        raise CirraError("GitHub pull request belongs to another repository")
    count = number(
        pr.get("changed_files"), "GitHub changed file count", 0, 1_000_000, integral=True
    )
    return sha(head.get("sha")), sha(base.get("sha")), count


def target_revision(client, branch_name):
    # PR base.sha can remain stale after a base-only push. Read the named branch
    # independently, within the already allowlisted repository.
    branch, _ = client.get("branches/" + quote(branch_name, safe=""))
    if record(branch).get("name") != branch_name:
        raise CirraError("GitHub returned a different target branch")
    return sha(record(branch.get("commit")).get("sha"))


def files_from(comparison, count):
    files = comparison.get("files")
    if not isinstance(files, list):
        raise CirraError("GitHub comparison has no file list")
    gaps, result = [], []
    if len(files) != count:
        gaps.append(
            f"GitHub comparison returned {len(files)} files; PR metadata reports {count}; "
            "comparison API coverage is limited to 300 files"
        )
    for item in files:
        record(item)
        path = path_name(item.get("filename"))
        patch = item.get("patch")
        if patch is not None:
            if not isinstance(patch, str):
                raise CirraError("GitHub patch must be text or null")
            additions = number(
                item.get("additions"), "GitHub additions", 0, 2**31 - 1, integral=True
            )
            deletions = number(
                item.get("deletions"), "GitHub deletions", 0, 2**31 - 1, integral=True
            )
            if (
                sum(line.startswith("+") for line in patch.splitlines()) != additions
                or sum(line.startswith("-") for line in patch.splitlines()) != deletions
            ):
                gaps.append(f"{path}: patch line counts do not cover the native file change")
                patch = None
        result.append(
            {
                "path": path,
                "old_path": item.get("previous_filename"),
                "status": item.get("status"),
                "patch": patch,
            }
        )
    return sorted(result, key=lambda item: item["path"]), gaps


def check_from(item, kind, head, observed):
    record(item)
    identifier = native_id(item.get("id"))
    if kind == "check_run":
        status = string(item.get("status"), "GitHub check status")
        raw_state = item.get("conclusion") if status == "completed" else status
        raw_state = raw_state if raw_state is not None else status
        states = {
            "success": "success",
            "failure": "failure",
            "timed_out": "failure",
            "action_required": "failure",
            "startup_failure": "failure",
            "cancelled": "cancelled",
            "neutral": "skipped",
            "skipped": "skipped",
            "queued": "pending",
            "in_progress": "pending",
            "waiting": "pending",
            "requested": "pending",
            "pending": "pending",
        }
        source = native_id(record(item.get("app")).get("id"))
        name, associated, started = item.get("name"), item.get("head_sha"), item.get("started_at")
        url = item.get("html_url")
    else:
        raw_state = item.get("state")
        states = {
            "success": "success",
            "failure": "failure",
            "error": "failure",
            "pending": "pending",
        }
        source = native_id(record(item.get("creator")).get("id"))
        name, associated, started = item.get("context"), head, item.get("created_at")
        url = item.get("target_url") or item.get("url")
    string(raw_state, "GitHub raw state")
    check = {
        "kind": kind,
        "source_id": source,
        "name": name,
        "native_id": identifier,
        "run_id": None,
        "attempt": None,
        "raw_state": raw_state,
        "normalized_state": states.get(raw_state, "unknown"),
        "associated_sha": associated,
        "provenance": {"trusted": False, "tested_sha": None, "parents": []},
        "trigger": "not_exposed",
        "url": url,
        "observed_at": observed,
        "started_at": started,
    }
    if kind == "check_run":
        suite = record(item.get("check_suite"))
        check["check_suite_id"] = native_id(suite.get("id"))
    return validate_check(check)


def checks_from(client, head, max_pages):
    checks, errors = [], []
    for kind, endpoint in (("check_run", "check-runs"), ("commit_status", "statuses")):
        seen, received, expected = set(), 0, 0
        for page in range(1, max_pages + 1):
            params = {"per_page": 100, "page": page}
            if kind == "check_run":
                params["filter"] = "all"
            try:
                data, more = client.get(f"commits/{head}/{endpoint}", params)
                observed = utc_now()
                if kind == "check_run":
                    record(data)
                    expected = max(
                        expected,
                        number(
                            data.get("total_count"),
                            "GitHub check count",
                            0,
                            1_000_000,
                            integral=True,
                        ),
                    )
                    data = data.get("check_runs")
                if not isinstance(data, list):
                    raise CirraError("GitHub check page is not a list")
                received += len(data)
                for item in data:
                    try:
                        check = check_from(item, kind, head, observed)
                        if check["native_id"] in seen:
                            raise CirraError("GitHub check pagination repeated a native ID")
                        seen.add(check["native_id"])
                        checks.append(check)
                    except CirraError:
                        errors.append(f"{endpoint}: malformed or duplicate native check record")
                if not more:
                    if received < expected:
                        errors.append(f"{endpoint}: incomplete check pagination")
                    break
                if page == max_pages:
                    errors.append(f"{endpoint}: check page limit reached")
            except CirraError as exc:
                errors.append(f"{endpoint}: {exc}")
                break
    return checks, sorted(set(errors))


def collect(repository, change, config, *, client=None, max_pages=10):
    repository_name(repository)
    number(change, "pull request number", 1, 2**31 - 1, integral=True)
    number(max_pages, "check page limit", 1, 100, integral=True)
    if config.reviewer_kind not in ("session", "claude"):
        raise CirraError("live collection requires a session or claude reviewer policy")
    client = client if client is not None else GitHubClient(repository)
    repo, _ = client.get()
    record(repo)
    repository_id = native_id(repo.get("id"))
    if string(repo.get("full_name"), "GitHub repository").lower() != repository.lower():
        raise CirraError("GitHub returned a different repository name")
    identity = {
        "provider": "github",
        "instance": "https://github.com",
        "repository_id": repository_id,
    }
    if identity not in config.repositories:
        raise CirraError("GitHub repository is not allowlisted by operator policy")
    for _ in range(3):
        pr, _ = client.get(f"pulls/{change}")
        head, _, count = revisions(pr, repository_id, change)
        branch_name = string(pr["base"].get("ref"), "GitHub target branch", 1024)
        before = head, branch_name, count
        target = target_revision(client, branch_name)
        comparison, _ = client.get(f"compare/{target}...{head}", {"per_page": 1, "page": 1})
        record(comparison)
        if sha(record(comparison.get("base_commit")).get("sha")) != target:
            raise CirraError("GitHub comparison does not match the pinned target")
        base = sha(record(comparison.get("merge_base_commit")).get("sha"))
        files, gaps = files_from(comparison, count)
        checks, errors = checks_from(client, head, max_pages)
        after, _ = client.get(f"pulls/{change}")
        after_head, _, after_count = revisions(after, repository_id, change)
        if (after_head, after["base"].get("ref"), after_count) != before:
            continue
        if target_revision(client, branch_name) != target:
            continue
        data = {
            "schema_version": 1,
            "change": {
                **identity,
                "change_id": str(change),
                "head_sha": head,
                "target_sha": target,
                "diff_base_sha": base,
            },
            "files": files,
            "coverage_gaps": gaps,
            "checks": checks,
            "collection": {
                "source": "github_rest",
                "repository": repository,
                "observed_at": utc_now(),
                "check_errors": errors,
            },
        }
        FixtureProvider.from_data(data, config)
        return data
    raise CirraError("GitHub pull request changed during collection; retry after it settles")


def export_capture(data, state_dir):
    content = json.dumps(data, indent=2, ensure_ascii=True) + "\n"
    if len(content.encode()) > 16 * 1024 * 1024:
        raise CirraError("captured snapshot exceeds the local 16 MiB input limit")
    directory = Path(state_dir).resolve() / "captures"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest(data)}.json"
    temporary = directory / f".{uuid.uuid4().hex}.tmp"
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return str(path)
