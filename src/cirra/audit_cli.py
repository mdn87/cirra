"""CLI entry points for production-audit attempts handed to an agent session."""

import json
from pathlib import Path

from cirra import audits
from cirra.config import load_config
from cirra.domain import CirraError
from cirra.store import Store


def register(commands):
    prepare = commands.add_parser(
        "prepare-audit", help="open or reuse an audit attempt and export its agent packet"
    )
    prepare.add_argument("--config", type=Path, required=True)
    prepare.add_argument("--tier", choices=("pr", "full", "release"), required=True)
    prepare.add_argument("--head", required=True, help="full commit ID to audit")
    prepare.add_argument("--base", help="full diff-base commit ID (pr tier only)")
    prepare.add_argument("--repository-id", help="allowlisted repository, when several")
    prepare.add_argument("--checkout", type=Path, required=True, help="local Git checkout")
    prepare.add_argument("--state-dir", type=Path, default=Path(".cirra"))
    imported = commands.add_parser(
        "import-audit", help="validate an audit report and store it against its attempt"
    )
    imported.add_argument("--attempt", required=True)
    imported.add_argument("--report", type=Path, required=True)
    imported.add_argument("--checkout", type=Path, required=True)
    imported.add_argument("--state-dir", type=Path, default=Path(".cirra"))


def run(args):
    if args.command == "prepare-audit":
        config = load_config(args.config)
        identity = audits.audit_identity(
            config.audit,
            args.tier,
            audits.select_repository(config, args.repository_id),
            args.head,
            args.base,
        )
        if audits.resolve(args.checkout, args.head) != args.head:
            raise CirraError("the checkout does not contain the head commit")
        if args.base and audits.resolve(args.checkout, args.base) != args.base:
            raise CirraError("the checkout does not contain the base commit")
        store = Store(args.state_dir)
        attempt, reused = audits.begin(store, identity)
        result = {**audits.summary(attempt), "reused": reused}
        if attempt["state"] == "prepared":
            packet = audits.make_packet(store, attempt, args.checkout)
            result["packet_path"] = audits.export_packet(packet, args.state_dir)
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "import-audit":
        if not (args.state_dir / "cirra.sqlite3").is_file():
            raise CirraError("state database does not exist; run prepare-audit first")
        result, reused = audits.import_report(
            Store(args.state_dir), args.attempt, args.report, args.checkout
        )
        print(json.dumps({**result, "reused": reused}, indent=2))
        return 0 if result["state"] == "complete" else 1
    return None
