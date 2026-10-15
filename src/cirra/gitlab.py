"""GitLab MR diff versions and native pipeline jobs, with revision rechecks."""

import re
from urllib.parse import quote

from cirra.domain import CirraError, number, path_name, sha, string, utc_now
from cirra.fixture import FixtureProvider, validate_check
from cirra.github import native_id, record
from cirra.transport import JSONClient, pages


def project_name(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", value
    ):
        raise CirraError("GitLab project must be namespace/project")
    if any(part in (".", "..") for part in value.split("/")):
        raise CirraError("invalid GitLab project")
    return value


class GitLabClient:
    def __init__(self, repository, *, token=None, timeout=60):
        self.repository = project_name(repository)
        self.prefix = "/api/v4/projects/" + quote(repository, safe="")
        self.http = JSONClient(
            "https://gitlab.com", {"PRIVATE-TOKEN": token} if token else {}, timeout=timeout
        )

    def get(self, route="", params=None):
        allowed = (
            r"(?:merge_requests(?:/[1-9][0-9]*(?:/(?:versions(?:/[1-9][0-9]*)?|pipelines|notes(?:/[1-9][0-9]*)?))?)?"
            r"|repository/branches/[^/]+|pipelines(?:/[1-9][0-9]*(?:/jobs)?)?)?"
        )
        if not re.fullmatch(allowed, route):
            raise CirraError("unsupported GitLab read endpoint")
        return self.http.request("GET", self.prefix + ("/" + route if route else ""), params)

    def note(self, change, body, identifier=None):
        number(change, "MR number", 1, 2**31 - 1, integral=True)
        route = f"{self.prefix}/merge_requests/{change}/notes"
        if identifier is not None:
            number(int(identifier), "note ID", 1, 2**63 - 1, integral=True)
            route += f"/{identifier}"
        return self.http.request("PUT" if identifier else "POST", route, body={"body": body})[0]


def revisions(client, change, repository_id):
    mr, _ = client.get(f"merge_requests/{change}")
    record(mr)
    if native_id(mr.get("project_id")) != repository_id or mr.get("iid") != change:
        raise CirraError("GitLab MR identity mismatch")
    refs = record(mr.get("diff_refs"))
    head = sha(refs.get("head_sha"))
    if sha(mr.get("sha")) != head:
        raise CirraError("GitLab MR diff is still being prepared; retry later")
    branch = string(mr.get("target_branch"), "target branch")
    target, _ = client.get("repository/branches/" + quote(branch, safe=""))
    return (
        head,
        sha(refs.get("base_sha")),
        sha(refs.get("start_sha")),
        sha(record(target.get("commit")).get("id")),
    )


def job_check(job, pipeline, repository_id):
    state = string(job.get("status"), "GitLab job status")
    states = {
        "success": "success",
        "failed": "failure",
        "canceled": "cancelled",
        "skipped": "skipped",
        "created": "pending",
        "pending": "pending",
        "running": "pending",
        "waiting_for_resource": "pending",
        "preparing": "pending",
        "manual": "pending",
        "scheduled": "pending",
    }
    associated = sha(record(job.get("commit")).get("id"))
    if associated != sha(pipeline.get("sha")):
        raise CirraError("GitLab job and pipeline revision mismatch")
    return validate_check(
        {
            "kind": "pipeline_job",
            "source_id": repository_id,
            "name": job.get("name"),
            "native_id": native_id(job.get("id")),
            "run_id": native_id(pipeline.get("id")),
            "attempt": None,
            "raw_state": state,
            "normalized_state": states.get(state, "unknown"),
            "associated_sha": associated,
            "provenance": {"trusted": False, "tested_sha": None, "parents": []},
            "trigger": pipeline.get("source", "not_exposed"),
            "url": job.get("web_url"),
            "observed_at": utc_now(),
            "started_at": job.get("started_at") or job.get("created_at"),
        }
    )


def collect(repository, change, config, *, client=None, max_pages=10):
    project_name(repository)
    number(change, "MR number", 1, 2**31 - 1, integral=True)
    number(max_pages, "page limit", 1, 100, integral=True)
    if config.reviewer_kind not in ("session", "claude"):
        raise CirraError("live collection requires a session or claude reviewer policy")
    client = client or GitLabClient(repository)
    project, _ = client.get()
    identifier = native_id(record(project).get("id"))
    if project.get("path_with_namespace", "").lower() != repository.lower():
        raise CirraError("GitLab returned a different project")
    identity = {"provider": "gitlab", "instance": "https://gitlab.com", "repository_id": identifier}
    if identity not in config.repositories:
        raise CirraError("GitLab repository is not allowlisted by operator policy")
    for _ in range(3):
        before = revisions(client, change, identifier)
        head, base, start, target = before
        versions = pages(client.get, f"merge_requests/{change}/versions", max_pages=max_pages)
        candidates = [
            item
            for item in versions
            if item.get("head_commit_sha") == head
            and item.get("base_commit_sha") == base
            and item.get("start_commit_sha") == start
        ]
        if not candidates:
            raise CirraError("GitLab has no collected diff version for the pinned MR revisions")
        version, _ = client.get(
            f"merge_requests/{change}/versions/{max(item['id'] for item in candidates)}"
        )
        if (
            version.get("head_commit_sha"),
            version.get("base_commit_sha"),
            version.get("start_commit_sha"),
        ) != (head, base, start):
            raise CirraError("GitLab diff version changed unexpectedly")
        raw_files = version.get("diffs")
        if not isinstance(raw_files, list):
            raise CirraError("GitLab diff version has no file list")
        gaps, files = [], []
        if (
            str(version.get("real_size")) != str(len(raw_files))
            or version.get("state") != "collected"
        ):
            gaps.append("GitLab diff version may be incomplete or limited")
        if start != target:
            gaps.append(
                "GitLab diff version uses an older target; "
                "refresh the MR diff before relying on coverage"
            )
        for item in raw_files:
            record(item)
            path = path_name(item.get("new_path"))
            old_path = path_name(item.get("old_path"))
            status = (
                "renamed"
                if item.get("renamed_file")
                else "added"
                if item.get("new_file")
                else "removed"
                if item.get("deleted_file")
                else "modified"
            )
            patch = item.get("diff")
            if item.get("too_large") or item.get("collapsed") or not patch:
                patch = None
            files.append(
                {
                    "path": path,
                    "old_path": old_path if status == "renamed" else None,
                    "status": status,
                    "patch": patch,
                }
            )
        checks, errors = [], []
        try:
            pipelines = pages(client.get, f"merge_requests/{change}/pipelines", max_pages=max_pages)
            if len(pipelines) > 100:
                raise CirraError("GitLab pipeline inspection limit reached")
            for pipeline in pipelines:
                pipeline, _ = client.get(f"pipelines/{pipeline['id']}")
                if pipeline.get("project_id") != int(identifier):
                    raise CirraError("GitLab pipeline belongs to another project")
                jobs = pages(
                    client.get,
                    f"pipelines/{pipeline['id']}/jobs",
                    params={"include_retried": "true"},
                    max_pages=max_pages,
                )
                checks.extend(job_check(job, pipeline, identifier) for job in jobs)
        except CirraError as exc:
            errors.append(str(exc))
        if revisions(client, change, identifier) != before:
            continue
        data = {
            "schema_version": 1,
            "change": {
                **identity,
                "change_id": str(change),
                "head_sha": head,
                "diff_base_sha": base,
                "target_sha": target,
            },
            "files": sorted(files, key=lambda item: item["path"]),
            "coverage_gaps": gaps,
            "checks": checks,
            "collection": {
                "source": "gitlab_rest",
                "repository": repository,
                "observed_at": utc_now(),
                "check_errors": errors,
            },
        }
        FixtureProvider.from_data(data, config)
        return data
    raise CirraError("GitLab MR changed during collection; retry after it settles")
