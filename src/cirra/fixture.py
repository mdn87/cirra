"""Load and validate local snapshots. Supplied provenance is not independent attestation."""

import json
from pathlib import Path

from cirra.domain import (
    CirraError,
    FileDiff,
    Snapshot,
    canonical,
    digest,
    fields,
    number,
    patch_lines,
    path_name,
    sha,
    string,
    timestamp,
)


class FixtureProvider:
    def __init__(self, path, config):
        raw = Path(path).read_bytes()
        if len(raw) > 16 * 1024 * 1024:
            raise CirraError("fixture exceeds 16 MiB")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise CirraError("fixture is not valid JSON") from exc
        self._load(data, config)

    @classmethod
    def from_data(cls, data, config):
        provider = cls.__new__(cls)
        provider._load(data, config)
        return provider

    def _load(self, data, config):
        fields(
            data,
            ["schema_version", "change", "files", "coverage_gaps", "checks"],
            ["reviewer_response", "collection"],
            label="fixture",
        )
        if config.reviewer_kind == "fake" and "reviewer_response" not in data:
            raise CirraError("fake reviewer requires reviewer_response in the fixture")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise CirraError("unsupported fixture schema version")
        change = fields(
            data["change"],
            [
                "provider",
                "instance",
                "repository_id",
                "change_id",
                "head_sha",
                "diff_base_sha",
                "target_sha",
            ],
            label="change",
        )
        for key in ("provider", "instance", "repository_id", "change_id"):
            string(change[key], key)
        for key in ("head_sha", "diff_base_sha", "target_sha"):
            sha(change[key])
        if {key: change[key] for key in ("provider", "instance", "repository_id")} not in (
            config.repositories
        ):
            raise CirraError("fixture repository is not allowlisted by operator policy")
        if not isinstance(data["coverage_gaps"], list) or not isinstance(data["files"], list):
            raise CirraError("files and coverage_gaps must be lists")
        gaps = [string(item, "coverage gap") for item in data["coverage_gaps"]]
        files = []
        seen = set()
        used_bytes = 0
        for item in data["files"]:
            fields(item, ["path", "old_path", "status", "patch"], label="file")
            path_name(item["path"])
            if item["path"] in seen:
                raise CirraError("duplicate file path in fixture")
            seen.add(item["path"])
            if item["old_path"] is not None:
                path_name(item["old_path"])
            string(item["status"], "file status")
            if item["status"] == "renamed" and item["old_path"] is None:
                raise CirraError("renamed files require an old_path")
            patch = item["patch"]
            if patch is not None and not isinstance(patch, str):
                raise CirraError("patch must be a string or null")
            old_lines = new_lines = frozenset()
            gap = None
            if item["status"] not in ("added", "modified", "removed", "renamed") or patch is None:
                gap = "unsupported or missing patch"
            elif used_bytes + len(patch.encode()) > config.max_diff_bytes:
                gap = "diff byte limit exceeded"
            else:
                try:
                    old_lines, new_lines = patch_lines(patch)
                    used_bytes += len(patch.encode())
                except CirraError as exc:
                    gap = str(exc)
            if gap:
                gaps.append(f"{item['path']}: {gap}")
                patch = None
            files.append(
                FileDiff(
                    path=item["path"],
                    old_path=item["old_path"],
                    status=item["status"],
                    patch=patch,
                    old_lines=old_lines,
                    new_lines=new_lines,
                    gap=gap,
                )
            )
        self.snapshot = Snapshot(
            **change,
            diff_digest=digest(data["files"]),
            files=tuple(files),
            coverage_gaps=tuple(gaps),
        )
        if not isinstance(data["checks"], list):
            raise CirraError("checks must be a list")
        self.checks = [validate_check(check) for check in data["checks"]]
        response = data.get("reviewer_response")
        self.response = response if isinstance(response, str) else canonical(response)
        self.response_digest = digest(self.response)
        self.collection = data.get("collection")
        self.check_errors = []
        if self.collection is not None:
            fields(
                self.collection,
                ["source", "repository", "observed_at", "check_errors"],
                label="collection",
            )
            if self.collection["source"] not in ("github_rest", "gitlab_rest", "forgejo_rest"):
                raise CirraError("unsupported collection source")
            string(self.collection["repository"], "collection repository")
            timestamp(self.collection["observed_at"])
            if not isinstance(self.collection["check_errors"], list):
                raise CirraError("collection check_errors must be a list")
            self.check_errors = [
                string(item, "collection error") for item in self.collection["check_errors"]
            ]

    def packet(self):
        return {
            "snapshot": self.snapshot.record(),
            "patches": [
                {"path": file.path, "old_path": file.old_path, "patch": file.patch}
                for file in self.snapshot.files
                if file.patch is not None
            ],
        }


def validate_check(check):
    fields(
        check,
        [
            "kind",
            "source_id",
            "name",
            "native_id",
            "run_id",
            "attempt",
            "raw_state",
            "associated_sha",
            "provenance",
            "trigger",
            "url",
            "observed_at",
            "started_at",
        ],
        ["normalized_state", "check_suite_id"],
        label="check",
    )
    check = dict(check)
    for key in ("kind", "source_id", "name", "native_id", "raw_state", "trigger", "url"):
        string(check[key], key)
    if check["kind"] not in ("check_run", "commit_status", "pipeline_job"):
        raise CirraError("unsupported check kind")
    if check["run_id"] is not None:
        string(check["run_id"], "run_id")
    if check["attempt"] is not None:
        number(check["attempt"], "attempt", 1, 1_000_000, integral=True)
    if "check_suite_id" in check and check["check_suite_id"] is not None:
        string(check["check_suite_id"], "check_suite_id")
    if "normalized_state" in check and check["normalized_state"] not in (
        "success",
        "failure",
        "pending",
        "skipped",
        "cancelled",
        "inaccessible",
        "unknown",
    ):
        raise CirraError("unsupported normalized check state")
    sha(check["associated_sha"])
    for key in ("observed_at", "started_at"):
        if key != "started_at" or check[key] is not None:
            check[key] = timestamp(check[key])
    proof = fields(
        check["provenance"],
        ["trusted", "tested_sha", "parents"],
        ["witness", "receipt_digest", "evidence", "definition_digest"],
        label="provenance",
    )
    for key in ("witness", "receipt_digest", "evidence", "definition_digest"):
        if key in proof:
            string(proof[key], key)
    if type(proof["trusted"]) is not bool or not isinstance(proof["parents"], list):
        raise CirraError("provenance requires a boolean trusted flag and parents list")
    if proof["tested_sha"] is not None:
        sha(proof["tested_sha"])
    for parent in proof["parents"]:
        sha(parent)
    return check
