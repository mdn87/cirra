"""List immutable worker evidence eligible for verified transfer to a cold archive."""

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from cirra.domain import CirraError


def cold_evidence(state_dir, *, days=30, now=None):
    """Relative regular files only. Keep active records, current receipts and replay keys."""
    root = Path(state_dir).resolve(strict=True)
    if days < 7:
        raise CirraError("cold evidence retention must be at least seven days")
    cutoff = (time.time() if now is None else now) - days * 86400

    def eligible(path):
        # Work and history names are generated locally. Never cross a link or a junction.
        return (
            path.is_file() and path.resolve() == path.absolute() and path.stat().st_mtime < cutoff
        )

    for kind in ("queue", "audit-queue"):
        for path in (root / kind / "archive").glob("*.json"):
            if not eligible(path):
                continue
            if kind == "queue":
                if (
                    not re.fullmatch("[a-f0-9]{64}", path.stem)
                    or not (root / kind / "completed" / path.name).is_file()
                ):
                    continue
            else:
                if not re.fullmatch("[a-f0-9]{32}", path.stem):
                    continue
                job = json.loads(path.read_text(encoding="utf-8"))
                if not re.fullmatch("[a-f0-9]{64}", job.get("work_key", "")):
                    continue
                latest = root / kind / "latest" / (job["work_key"] + ".json")
                if (
                    not latest.is_file()
                    or json.loads(latest.read_text(encoding="utf-8"))["accepted_at"]
                    < job["accepted_at"]
                ):
                    continue
            yield path.relative_to(root)

    # These histories contain content-addressed snapshots. Current receipts and
    # durable compact delivery/completion markers are never exported by this selector.
    for kind in (
        "queue",
        "audit-queue",
        "observer",
        "ci",
        "audit-publication",
        "worker-usage",
        "audit-usage",
    ):
        history = root / kind / "history"
        for path in history.glob("*/*.json"):
            if re.fullmatch("[a-f0-9]{64}", path.stem) and eligible(path):
                yield path.relative_to(root)
        if kind == "queue":
            for path in history.glob("*-*.json"):
                if re.fullmatch("[a-f0-9]{64}-[a-f0-9]{32}", path.stem) and eligible(path):
                    yield path.relative_to(root)

    for attempt in (root / "observer").glob("*-*"):
        if not attempt.is_dir() or not re.fullmatch("[a-f0-9]{64}-[a-f0-9]{32}", attempt.name):
            continue
        receipt = attempt.parent / (attempt.name[:64] + ".json")
        if not eligible(receipt):
            continue
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        if saved.get("state") not in ("observed", "published"):
            continue
        # Final witnesses still refer to the container log and source hash. Park
        # only the source archives and Git object data used to prepare the sandbox.
        for archive in (attempt / "source.tar", attempt / "input" / "source.tar"):
            if eligible(archive):
                yield archive.relative_to(root)
        objects = attempt / "git" / "objects"
        for path in objects.glob("*/*"):
            if eligible(path):
                yield path.relative_to(root)

    # Audit agents append to work/agent.log. A final stored attempt and the
    # removal of its worker checkout distinguish a closed log from idle work.
    attempts = root / "audit-attempts"
    database = root / "cirra.sqlite3"
    if not attempts.is_dir() or not database.is_file() or database.resolve() != database:
        return
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audits'"
        ).fetchone():
            return
        rows = connection.execute(
            "SELECT attempt_id FROM audits "
            "WHERE state IN ('complete','failed','cancelled','superseded') "
            "AND (completed_at IS NULL OR completed_at < ?)",
            (cutoff,),
        )
        for (identifier,) in rows:
            if not isinstance(identifier, str) or not re.fullmatch("[a-f0-9]{32}", identifier):
                continue
            directory = attempts / identifier
            log = directory / "work" / "agent.log"
            if (
                eligible(log)
                and not (directory / "checkout").exists()
                and directory.stat().st_mtime < cutoff
            ):
                yield log.relative_to(root)


def manifest(state_dir, days, stream):
    for path in cold_evidence(state_dir, days=days):
        stream.write(os.fsencode(path.as_posix()) + b"\0")
