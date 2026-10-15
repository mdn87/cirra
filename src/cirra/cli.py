"""Local fixture reviews, agent session handoffs, and saved report inspection."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from cirra import __version__
from cirra.config import load_config
from cirra.coordinator import review
from cirra.domain import CirraError
from cirra.fixture import FixtureProvider
from cirra.report import export_report, render_markdown
from cirra.session import export_packet, import_response, prepare
from cirra.store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cirra advisory review coordination")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("review", "review a local fixture using its deterministic fake response"),
        ("prepare-review", "export a packet for an existing agent session"),
        ("import-review", "validate and record an agent session response"),
    ):
        run = commands.add_parser(name, help=help_text)
        run.add_argument("--fixture", type=Path, required=True)
        run.add_argument(
            "--config", type=Path, required=True, help="explicit trusted operator policy"
        )
        run.add_argument("--state-dir", type=Path, default=Path(".cirra"))
        if name == "import-review":
            run.add_argument("--run", required=True)
            run.add_argument("--response", type=Path, required=True)
        else:
            run.add_argument(
                "--rereview", action="store_true", help="explicitly create another reviewer attempt"
            )
    show = commands.add_parser("show", help="show a saved report without invoking a reviewer")
    show.add_argument("--run", required=True)
    show.add_argument("--state-dir", type=Path, default=Path(".cirra"))
    show.add_argument("--format", choices=("markdown", "json"), default="markdown")
    show.add_argument(
        "--export", action="store_true", help="restore JSON/Markdown exports from SQLite"
    )
    args = parser.parse_args(argv)
    try:
        if args.command != "show":
            config = load_config(args.config)
            provider = FixtureProvider(args.fixture, config)
            if args.command == "import-review" and not (args.state_dir / "cirra.sqlite3").is_file():
                raise CirraError("state database does not exist; run prepare-review first")
            store = Store(args.state_dir)
            packet_paths = {}
            if args.command == "prepare-review":
                packet, report, reused = prepare(provider, config, store, rereview=args.rereview)
                packet_paths = export_packet(packet, args.state_dir)
            elif args.command == "import-review":
                report, reused = import_response(provider, config, store, args.run, args.response)
            else:
                report, reused = review(provider, config, store, rereview=args.rereview)
            paths = export_report(report, args.state_dir)
            print(
                json.dumps(
                    {
                        "run_id": report["run_id"],
                        "status": report["status"],
                        "review_outcome": report["review"]["outcome"],
                        "reused": reused,
                        **packet_paths,
                        **paths,
                    },
                    indent=2,
                )
            )
            return 0 if args.command == "prepare-review" or report["status"] == "complete" else 1
        if not (args.state_dir / "cirra.sqlite3").is_file():
            raise CirraError("state database does not exist; run review first")
        report = Store(args.state_dir).latest_report(args.run)
        if args.export:
            export_report(report, args.state_dir)
        print(json.dumps(report, indent=2) if args.format == "json" else render_markdown(report))
        return 0
    except (CirraError, OSError, sqlite3.Error, UnicodeError) as exc:
        # Do not print raw provider payloads, reviewer exceptions, or policy contents.
        message = (
            str(exc) if isinstance(exc, CirraError) else f"local I/O failed ({type(exc).__name__})"
        )
        print(f"cirra: {message}", file=sys.stderr)
        return 2
