"""Find missed PR/MR revisions using provider reads, independent of Actions."""

from urllib.parse import quote

from cirra.domain import CirraError, digest, number, sha, utc_now
from cirra.events import trusted_change
from cirra.github import GitHubClient, native_id, record, target_revision
from cirra.gitlab import GitLabClient
from cirra.transport import pages


def discover(route, token=None):
    client = (GitHubClient if route.provider == "github" else GitLabClient)(
        route.repository, token=token
    )
    repository, _ = client.get()
    if native_id(record(repository).get("id")) != route.repository_id:
        raise CirraError("reconciliation repository identity changed")
    github = route.provider == "github"
    items = pages(
        client.get,
        "pulls" if github else "merge_requests",
        params={"state": "open" if github else "opened"},
    )
    targets, changes = {}, []
    for item in items:
        if item.get("draft", item.get("work_in_progress", False)):
            continue
        if not trusted_change(route.provider, item):
            continue  # same author rule as the webhook receiver
        change = number(
            item.get("number" if github else "iid"), "change number", 1, 2**31 - 1, integral=True
        )
        if github:
            base = record(item.get("base"))
            if native_id(record(base.get("repo")).get("id")) != route.repository_id:
                raise CirraError("reconciliation pull request belongs to another repository")
            head, branch = sha(record(item.get("head")).get("sha")), base.get("ref")
        else:
            if str(item.get("project_id")) != route.repository_id:
                raise CirraError("reconciliation merge request belongs to another repository")
            head, branch = sha(item.get("sha")), item.get("target_branch")
        if not isinstance(branch, str) or not branch:
            raise CirraError("reconciliation target branch is missing")
        if branch not in targets:
            if github:
                targets[branch] = target_revision(client, branch)
            else:
                target, _ = client.get("repository/branches/" + quote(branch, safe=""))
                if record(target).get("name") != branch:
                    raise CirraError("reconciliation target branch changed identity")
                targets[branch] = sha(record(target.get("commit")).get("id"))
        revision = {"head": head, "target": targets[branch]}
        if getattr(getattr(route, "policy", None), "merge_gate", None):
            revision["target_branch"] = branch
        changes.append((change, revision))
    return changes


def sync(config, queue, credential, *, route_name=None, discover_changes=discover):
    results = []
    for name, route in config.routes.items():
        if route_name is not None and name != route_name:
            continue
        try:
            changes = discover_changes(route, credential(route.reader_env))
            queued, tested = [], []
            gate = getattr(getattr(route, "policy", None), "merge_gate", None)
            for change, revision in changes:
                # CI tests the head. A required review also covers a specific target revision.
                target = (
                    revision["target"]
                    if gate and revision.get("target_branch") == gate.branch
                    else None
                )
                if queue.tested_head(route, change, revision["head"], review_target=target):
                    tested.append(change)
                    continue
                queued.append(
                    queue.request(
                        route,
                        change,
                        source="reconcile",
                        revision=(digest(revision) if target else revision["head"]),
                    )
                )
            entry = {"route": name, "jobs": queued}
            if tested:
                entry["already_tested"] = tested
            branch_errors = {}
            for branch in getattr(route, "branches", ()):
                try:
                    data = capture_branch(route, branch, credential(route.reader_env))
                    queued.append(
                        queue.request_branch(route, branch, revision=data["change"]["head_sha"])
                    )
                except (CirraError, OSError, ValueError) as exc:
                    branch_errors[branch] = (
                        str(exc) if isinstance(exc, CirraError) else "branch capture failed"
                    )
            if branch_errors:
                entry["branch_errors"] = branch_errors
            results.append(entry)
        except (CirraError, OSError, ValueError) as exc:
            results.append(
                {
                    "route": name,
                    "error": str(exc)
                    if isinstance(exc, CirraError)
                    else "provider reconciliation failed",
                }
            )
    if route_name is not None and route_name not in config.routes:
        raise CirraError("unknown service route")
    return results


def capture_branch(route, branch, token=None):
    if branch not in route.branches:
        raise CirraError("branch is not allowlisted on this route")
    client = (GitHubClient if route.provider == "github" else GitLabClient)(
        route.repository, token=token
    )
    repository, _ = client.get()
    if native_id(record(repository).get("id")) != route.repository_id:
        raise CirraError("branch CI repository identity changed")
    if route.provider == "github":
        head = target_revision(client, branch)
    else:
        target, _ = client.get("repository/branches/" + quote(branch, safe=""))
        if record(target).get("name") != branch:
            raise CirraError("branch CI target changed identity")
        head = sha(record(target.get("commit")).get("id"))
    return {
        "change": {
            "provider": route.provider,
            "instance": route.instance,
            "repository_id": route.repository_id,
            "change_id": "branch:" + branch,
            "head_sha": head,
            "target_sha": head,
            "diff_base_sha": head,
        },
        "collection": {"repository": route.repository, "branch": branch, "observed_at": utc_now()},
        "checks": [],
    }
