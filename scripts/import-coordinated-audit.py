"""Import an accepted session audit while holding the coordinated worker's ownership.

Pause both workers before invoking this operator command on the current data
host; keep receivers and voters running. Resume the current worker first, then
the standby. This command never promotes a restored copy or publishes a check.
"""

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

from cirra import audits
from cirra.coordination import connect
from cirra.data_authority import DataAuthority
from cirra.domain import CirraError, canonical, digest
from cirra.quorum import QuorumUnavailable
from cirra.quorum_budget import enrollment_locks
from cirra.service_config import load_service
from cirra.store import Store
from cirra.vendor.production_audit.scripts import validate_report


def import_coordinated(config, directory, attempt_id, report, expected_digest, credential):
    selector = Path(directory).absolute()
    if not selector.is_symlink() or not re.fullmatch(r"[A-Za-z0-9_-]{8,140}", attempt_id):
        raise CirraError("coordinated import requires a managed selector and an existing attempt")
    selected = selector.resolve(strict=True)
    versions = selector.with_name(selector.name + ".restores")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", selected.name)
        or selected != versions / selected.name
        or not (selected / "cirra.sqlite3").is_file()
    ):
        raise CirraError("coordinated import requires the selected physical state version")
    checkout = selected / "audit-attempts" / attempt_id / "checkout"
    if checkout.resolve(strict=True) != checkout:
        raise CirraError("the audit checkout escaped the selected private state")
    source = Path(report)
    data = audits.read_report(source)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest) or digest(data) != expected_digest:
        raise CirraError("the report differs from the reviewed digest")
    # Freeze the exact accepted JSON before validation and import. A later edit
    # to the operator's original cannot substitute another report during import.
    with tempfile.TemporaryDirectory(prefix="cirra-audit-import-") as temporary:
        frozen = Path(temporary) / source.name
        frozen.write_text(canonical(data) + "\n", encoding="utf-8")
        frozen.chmod(0o600)
        frozen.with_suffix(".md").write_bytes(source.with_suffix(".md").read_bytes())
        frozen.with_suffix(".md").chmod(0o600)
        problems = validate_report.file_problems(frozen, data) + validate_report.validate(
            data, checkout
        )
        if problems:
            raise CirraError("the accepted report does not pass the native validator")
        with enrollment_locks(selected):
            if selector.resolve(strict=True) != selected:
                raise QuorumUnavailable("the selected private state changed before import")
            inbox = connect(config, selected, credential)
            if inbox is None:
                raise CirraError("this command requires an enrolled coordinated service")
            inbox.check()
            authority = DataAuthority(inbox)
            if not authority.can_recover_backup():
                raise QuorumUnavailable("import requires the original unfrozen authoritative state")
            owner = inbox.quorum.acquire()
            if owner is None:
                raise QuorumUnavailable(
                    "stop the workers and wait for their publishing lease to release"
                )
            with owner.maintaining():
                if selector.resolve(strict=True) != selected:
                    raise QuorumUnavailable(
                        "the selected private state changed during import admission"
                    )
                if not authority.can_recover_backup():
                    raise QuorumUnavailable("authoritative state changed during import admission")
                authority.activate(owner)
                owner.compare({}, {})
                store = Store(selected)
                result, reused = audits.import_report(store, attempt_id, frozen, checkout)
                authority.checkpoint(owner)
                saved = audits.get(store, attempt_id)
                if saved["report_digest"] != expected_digest:
                    raise CirraError("the stored audit differs from the accepted report")
                return {
                    "attempt_id": attempt_id,
                    "state": result["state"],
                    "report_digest": saved["report_digest"],
                    "reused": reused,
                    "selector": selected.name,
                    "authority_checkpointed": True,
                }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--expected-report-digest", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = import_coordinated(
            load_service(args.service_config),
            args.state_dir,
            args.attempt,
            args.report,
            args.expected_report_digest,
            os.environ.get,
        )
    except (CirraError, OSError, ValueError):
        # Detailed reports and credential-bearing paths stay in the private role.
        print(
            json.dumps(
                {"state": "refused", "reason": "coordinated audit import prerequisites failed"}
            )
        )
        return 1
    print(json.dumps(result))
    return 0 if result["state"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
