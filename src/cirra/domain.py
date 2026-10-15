"""Versioned local records and validation shared by the offline adapters."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath


class CirraError(Exception):
    """An actionable input or state error safe to display to the operator."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utc_now():
    return datetime.now(UTC).isoformat()


def fields(value, required, optional=(), *, label="record"):
    if not isinstance(value, dict):
        raise CirraError(f"{label} must be an object")
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if missing or extra:
        raise CirraError(f"{label}: missing keys {sorted(missing)}; unknown keys {sorted(extra)}")
    return value


def string(value, label, maximum=4096):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise CirraError(f"{label} must be a nonempty string of at most {maximum} characters")
    if any(ord(char) < 32 for char in value):
        raise CirraError(f"{label} contains control characters")
    return value


def number(value, label, minimum, maximum, *, integral=False):
    allowed = (int,) if integral else (int, float)
    if type(value) not in allowed or not minimum <= value <= maximum:
        raise CirraError(f"{label} must be between {minimum} and {maximum}")
    return value


def path_name(value):
    string(value, "file path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or ":" in value
        or any(part in (".", "..", "") for part in value.split("/"))
    ):
        raise CirraError("file paths must be relative POSIX paths without traversal")
    return value


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", value):
        raise CirraError("revision must be a full lowercase Git object ID")
    return value


def timestamp(value):
    string(value, "timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.astimezone(UTC).isoformat()
    except ValueError as exc:
        raise CirraError("timestamp must be ISO 8601 with a timezone") from exc


@dataclass(frozen=True)
class FileDiff:
    path: str
    old_path: str | None
    status: str
    patch: str | None
    old_lines: frozenset[int]
    new_lines: frozenset[int]
    gap: str | None

    def summary(self):
        return {
            "path": self.path,
            "old_path": self.old_path,
            "status": self.status,
            "patch_digest": digest(self.patch),
            "gap": self.gap,
        }


@dataclass(frozen=True)
class Snapshot:
    provider: str
    instance: str
    repository_id: str
    change_id: str
    head_sha: str
    diff_base_sha: str
    target_sha: str
    diff_digest: str
    files: tuple[FileDiff, ...]
    coverage_gaps: tuple[str, ...]

    @property
    def change_key(self):
        return digest([self.provider, self.instance, self.repository_id, self.change_id])

    def record(self):
        value = asdict(self)
        value["files"] = [file.summary() for file in self.files]
        value["coverage_gaps"] = list(self.coverage_gaps)
        return value


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$")


def patch_lines(patch):
    """Validate complete unified hunks and retain only actually present locations."""
    old_lines, new_lines = set(), set()
    remaining_old = remaining_new = 0
    old = new = 0
    found = False
    for line in patch.splitlines():
        match = HUNK.match(line)
        if match:
            if remaining_old or remaining_new:
                raise CirraError("truncated hunk")
            old, old_count, new, new_count = match.groups()
            old, new = int(old), int(new)
            remaining_old = int(old_count) if old_count is not None else 1
            remaining_new = int(new_count) if new_count is not None else 1
            if (remaining_old and old == 0) or (remaining_new and new == 0):
                raise CirraError("invalid hunk range")
            found = True
            continue
        if line == "\\ No newline at end of file":
            continue
        if not found or not line or line[0] not in " +-":
            raise CirraError("unsupported patch format")
        if line[0] in " -":
            remaining_old -= 1
            old_lines.add(old)
            old += 1
        if line[0] in " +":
            remaining_new -= 1
            new_lines.add(new)
            new += 1
        if remaining_old < 0 or remaining_new < 0:
            raise CirraError("hunk length mismatch")
    if not found or remaining_old or remaining_new:
        raise CirraError("missing or truncated hunks")
    return frozenset(old_lines), frozenset(new_lines)
