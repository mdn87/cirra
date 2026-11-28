"""Bounded operator commands on the original authoritative private-state version.

Both workers must be paused. Receivers and voters stay up. This entry point
does not promote a standby, reset admission counters, or bypass native gates.
"""

import argparse
import io
import json
import os
import re
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

from cirra import audit_cli, audits, extensions
from cirra.config import load_config
from cirra.coordination import connect
from cirra.data_authority import DataAuthority
from cirra.domain import CirraError
from cirra.locking import change_lock
from cirra.quorum import QuorumUnavailable
from cirra.service_config import load_service
from cirra.store import Store
from cirra.transport import RemoteUncertain

COMMANDS = frozenset(
    ("request-audit", "cancel-audit", "retry-audit-publication", "plan-release", "dispatch-release")
)


class CommandParsers:
    """Reuse native argument contracts while exposing only these operator commands."""

    def __init__(self, commands):
        self.commands = commands

    def add_parser(self, name, **options):
        if name in COMMANDS:
            return self.commands.add_parser(name, **options)
        return argparse.ArgumentParser(add_help=False)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--service-config", type=Path, required=True)
    commands = CommandParsers(result.add_subparsers(dest="command", required=True))
    audit_cli.register(commands)
    extensions.register(commands)
    return result


def selected_state(directory):
    selector = Path(directory).absolute()
    if not selector.is_symlink():
        raise CirraError("coordinated operations require a managed state selector")
    selected = selector.resolve(strict=True)
    versions = selector.with_name(selector.name + ".restores")
    if (
        not re.fullmatch("[0-9a-f]{64}", selected.name)
        or selected != versions / selected.name
        or not (selected / "cirra.sqlite3").is_file()
    ):
        raise CirraError("coordinated operations require the selected physical state version")
    # Refuse a schema mismatch before any native handler can open a writable Store.
    Store(selected, readonly=True)
    return selector, selected


def check_policy(args, config):
    if args.command not in ("request-audit", "plan-release", "dispatch-release"):
        return
    if not args.config:
        raise CirraError("coordinated commands require the enrolled route's --config")
    policy = load_config(args.config)
    routes = [route for route in config.routes.values() if route.policy.digest == policy.digest]
    if not routes:
        raise CirraError("operator policy differs from the enrolled service routes")
    if args.command == "request-audit":
        repository = audits.select_repository(policy, args.repository_id)
    elif args.command == "plan-release":
        repository = Store(args.state_dir, readonly=True).latest_report(args.run)["snapshot"]
    else:
        request = extensions.read_json(args.request)
        repository = request.get("snapshot") if isinstance(request, dict) else None
    if not isinstance(repository, dict) or not any(
        all(
            repository.get(name) == getattr(route, name)
            for name in ("provider", "instance", "repository_id")
        )
        for route in routes
    ):
        raise CirraError("operator repository has no matching enrolled route")
    if args.command == "dispatch-release" and args.retry_uncertain:
        raise CirraError("coordinated dispatch permits reconcile, not uncertain-write retries")


def operate(args, config, credential, capture_change):
    """Return a command receipt only after the original host checkpoints the outcome."""
    if args.command not in COMMANDS:
        raise CirraError("command is not available through coordinated operations")
    check_policy(args, config)
    selector, selected = selected_state(args.state_dir)
    # AuditQueue takes its own audit-queue lock. Holding enrollment_locks here
    # would self-deadlock. The worker lock excludes the writer/backup/transfer;
    # native queue methods retain their per-operation admission lock.
    with change_lock(selected / "queue" / "locks", "worker", 0):
        if selector.resolve(strict=True) != selected:
            raise QuorumUnavailable("selected private state changed before operator admission")
        inbox = connect(config, selected, credential)
        if inbox is None:
            raise CirraError("coordinated operations require an enrolled service")
        inbox.check()
        authority = DataAuthority(inbox)
        if not authority.can_recover_backup():
            raise QuorumUnavailable("operation requires original unfrozen authoritative state")
        owner = inbox.quorum.acquire()
        if owner is None:
            raise QuorumUnavailable("pause both workers and wait for their publishing lease")
        with owner.maintaining():
            if selector.resolve(strict=True) != selected or not authority.can_recover_backup():
                raise QuorumUnavailable("selected authority changed during operator admission")
            authority.activate(owner)
            owner.compare({}, {})
            # Resolve the managed selector once; never let a mid-operation pointer
            # switch direct a native handler to another version.
            native = argparse.Namespace(**vars(args))
            native.state_dir = selected
            output = io.StringIO()
            outcome, error, uncertain = None, None, False
            try:
                with redirect_stdout(output):
                    code = audit_cli.run(native)
                    if code is None:
                        code = extensions.run(native, credential, capture_change)
                outcome = json.loads(output.getvalue())
            except (CirraError, OSError, ValueError, sqlite3.Error) as exc:
                code, error = 1, type(exc).__name__
                uncertain = isinstance(exc, RemoteUncertain)
            receipt = {
                "command": args.command,
                "selector": selected.name,
                "state": "uncertain" if uncertain else "complete" if code == 0 else "failed",
                "operation_exit_code": code,
                "result": outcome,
                "authority_checkpointed": False,
            }
            if error:
                receipt.update(
                    error_type=error,
                    reason="native command refused or failed after admission; effects may exist",
                )
            try:
                if selector.resolve(strict=True) != selected:
                    raise QuorumUnavailable("selected private state changed during operation")
                authority.checkpoint(owner)
            except (CirraError, OSError, ValueError, sqlite3.Error):
                return {
                    **receipt,
                    "state": "outcome_unconfirmed",
                    "exit_code": 2,
                    "recovery": (
                        "inspect the authoritative state; reconcile external dispatches, "
                        "and replay only the same local operation"
                    ),
                }
            return {**receipt, "authority_checkpointed": True, "exit_code": code}


def main(argv=None):
    from cirra.cli import capture_change, token_from

    args = parser().parse_args(argv)
    os.umask(0o077)
    try:
        receipt = operate(args, load_service(args.service_config), token_from, capture_change)
    except (CirraError, OSError, ValueError, sqlite3.Error):
        print(json.dumps({"state": "refused", "reason": "coordinated prerequisites failed"}))
        return 1
    print(json.dumps(receipt))
    return receipt["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
