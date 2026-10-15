"""Explicit-origin Forgejo read adapter; native workflows remain provider-owned."""

import re

from cirra.domain import CirraError, number, sha, utc_now
from cirra.fixture import FixtureProvider, validate_check
from cirra.github import files_from, native_id, repository_name, revisions
from cirra.transport import JSONClient


class ForgejoClient:
    def __init__(self, instance, repository, *, token=None, timeout=60):
        if not instance:
            raise CirraError("Forgejo requires an explicit --instance HTTPS origin")
        self.repository = repository_name(repository)
        self.http = JSONClient(
            instance, {"Authorization": f"token {token}"} if token else {}, timeout=timeout
        )
        self.instance = self.http.origin
        self.prefix = "/api/v1/repos/" + repository

    def get(self, route="", params=None):
        if not re.fullmatch(
            r"(?:pulls/[1-9][0-9]*(?:/files|\.diff)?|commits/[a-f0-9]{40,64}/statuses)?", route
        ):
            raise CirraError("unsupported Forgejo read endpoint")
        params = dict(params or {})
        if "per_page" in params:
            params["limit"] = params.pop("per_page")
        return self.http.request(
            "GET",
            self.prefix + ("/" + route if route else ""),
            params,
            raw_text=route.endswith(".diff"),
        )


def diff_patches(raw):
    """Only accept unambiguous unquoted Git file headers; record other files as gaps."""
    result = {}
    for section in re.split(r"(?m)^diff --git ", raw)[1:]:
        lines = section.splitlines()
        old = next((line[4:] for line in lines if line.startswith("--- ")), None)
        new = next((line[4:] for line in lines if line.startswith("+++ ")), None)
        path = (
            new[2:]
            if new and new.startswith("b/")
            else old[2:]
            if old and old.startswith("a/")
            else None
        )
        hunk = next((i for i, line in enumerate(lines) if line.startswith("@@ ")), None)
        if path is not None and hunk is not None and path not in result:
            result[path] = "\n".join(lines[hunk:]) + "\n"
    return result


def collect(repository, change, config, *, client, max_pages=10):
    repository_name(repository)
    number(change, "pull request number", 1, 2**31 - 1, integral=True)
    number(max_pages, "page limit", 1, 100, integral=True)
    repo, _ = client.get()
    identifier = native_id(repo.get("id"))
    if repo.get("full_name", "").lower() != repository.lower():
        raise CirraError("Forgejo returned a different repository")
    identity = {"provider": "forgejo", "instance": client.instance, "repository_id": identifier}
    if identity not in config.repositories:
        raise CirraError("Forgejo repository is not allowlisted by operator policy")
    for _ in range(3):
        before, _ = client.get(f"pulls/{change}")
        head, target, count = revisions(before, identifier, change)
        base = sha(before.get("merge_base"))
        raw, _ = client.get(f"pulls/{change}.diff")
        patches = diff_patches(raw)
        native_files, checks, errors, gaps = [], [], [], []
        for page in range(1, max_pages + 1):
            items, more = client.get(f"pulls/{change}/files", {"page": page, "per_page": 100})
            if not isinstance(items, list):
                raise CirraError("Forgejo files page is not a list")
            native_files.extend(
                {
                    **item,
                    "status": {"changed": "modified", "deleted": "removed"}.get(
                        item.get("status"), item.get("status")
                    ),
                    "patch": patches.get(item.get("filename")),
                    "previous_filename": item.get("previous_filename") or None,
                }
                for item in items
            )
            if not more:
                break
            if page == max_pages:
                gaps.append("Forgejo file page limit reached")
        if len(native_files) != count:
            gaps.append(f"Forgejo returned {len(native_files)} files; metadata reports {count}")
        files, file_gaps = files_from({"files": native_files}, len(native_files))
        gaps.extend(gap.replace("GitHub", "Forgejo") for gap in file_gaps)
        seen = set()
        try:
            for page in range(1, max_pages + 1):
                statuses, more = client.get(
                    f"commits/{head}/statuses", {"page": page, "per_page": 100}
                )
                if not isinstance(statuses, list):
                    raise CirraError("Forgejo status page is not a list")
                for status in statuses:
                    creator = status.get("creator")
                    source = (
                        native_id(creator.get("id")) if isinstance(creator, dict) else "not_exposed"
                    )
                    if source == "not_exposed":
                        errors.append(
                            "Forgejo omitted the creator identity for native status evidence"
                        )
                    raw_state = status.get("status")
                    item = validate_check(
                        {
                            "kind": "commit_status",
                            "source_id": source,
                            "name": status.get("context"),
                            "native_id": native_id(status.get("id")),
                            "run_id": None,
                            "attempt": None,
                            "raw_state": raw_state,
                            "normalized_state": {
                                "success": "success",
                                "failure": "failure",
                                "error": "failure",
                                "pending": "pending",
                            }.get(raw_state, "unknown"),
                            "associated_sha": head,
                            "provenance": {"trusted": False, "tested_sha": None, "parents": []},
                            "trigger": "not_exposed",
                            "url": status.get("target_url") or status.get("url"),
                            "observed_at": utc_now(),
                            "started_at": status.get("created_at"),
                        }
                    )
                    if item["native_id"] in seen:
                        raise CirraError("Forgejo pagination repeated a status")
                    seen.add(item["native_id"])
                    checks.append(item)
                if not more:
                    break
                if page == max_pages:
                    raise CirraError("Forgejo status page limit reached")
        except CirraError as exc:
            errors.append(str(exc))
        after, _ = client.get(f"pulls/{change}")
        if (
            revisions(after, identifier, change) != (head, target, count)
            or after.get("merge_base") != base
        ):
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
            "checks": checks,
            "coverage_gaps": gaps,
            "collection": {
                "source": "forgejo_rest",
                "repository": repository,
                "observed_at": utc_now(),
                "check_errors": sorted(set(errors)),
            },
        }
        FixtureProvider.from_data(data, config)
        return data
    raise CirraError("Forgejo pull request changed during collection; retry after it settles")
