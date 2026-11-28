"""Requeue an inspected branch-CI failure on the original authoritative data host.

Pause both workers, keep receivers running, and provide the digest of the exact
job inspected. This command preserves history and does not refund prior charges.
"""

import argparse
import json
import os
import re
from pathlib import Path

from cirra.coordination import connect
from cirra.data_authority import DataAuthority
from cirra.domain import CirraError, digest
from cirra.job_queue import Queue
from cirra.locking import change_lock
from cirra.quorum import QuorumUnavailable
from cirra.service_config import load_service


def requeue_branch(config, directory, identifier, expected_digest, reason, credential):
    selector = Path(directory).absolute()
    if not selector.is_symlink() or not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise CirraError("requeue requires a managed selector and exact job identifier")
    selected = selector.resolve(strict=True)
    if (
        not re.fullmatch(r"[0-9a-f]{64}", selected.name)
        or selected != selector.with_name(selector.name + ".restores") / selected.name
        or not (selected / "cirra.sqlite3").is_file()
    ):
        raise CirraError("requeue requires the selected physical state version")
    queue = Queue(selected, config.max_jobs)
    with change_lock(queue.directory / "locks", "worker", 0):
        inbox = connect(config, selected, credential)
        if inbox is None:
            raise CirraError("requeue requires enrolled coordinated state")
        inbox.check()
        authority = DataAuthority(inbox)
        if not authority.can_recover_backup():
            raise QuorumUnavailable("requeue requires the original unfrozen authoritative state")
        owner = inbox.quorum.acquire()
        if owner is None:
            raise QuorumUnavailable("pause both workers before coordinated requeue")
        with owner.maintaining():
            if selector.resolve(strict=True) != selected or not authority.can_recover_backup():
                raise QuorumUnavailable("selected authority changed before requeue")
            authority.activate(owner)
            job = queue.review_record(queue.directory / (identifier + ".json"))
            route = config.routes.get(job["route"])
            if digest(job) != expected_digest:
                raise CirraError("job changed since inspection")
            if (
                route is None
                or job["state"] != "attention"
                or job["hint"].get("source") != "branch"
                or job["hint"].get("branch") not in route.branches
                or not route.observer
            ):
                raise CirraError("only an inspected allowlisted branch-CI attention job may resume")
            result = queue.requeue(identifier, route, reason)
            authority.checkpoint(owner)
            saved = queue.review_record(queue.directory / (identifier + ".json"))
            if saved != result or saved["state"] != "queued":
                raise CirraError("requeued job failed readback")
            return {
                "job_id": identifier,
                "state": "queued",
                "selector": selected.name,
                "authority_checkpointed": True,
                "previous_digest": expected_digest,
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--expected-job-digest", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = requeue_branch(
            load_service(args.service_config),
            args.state_dir,
            args.job,
            args.expected_job_digest,
            args.reason,
            os.environ.get,
        )
    except (CirraError, OSError, ValueError):
        print(
            json.dumps({"state": "refused", "reason": "coordinated requeue prerequisites failed"})
        )
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
