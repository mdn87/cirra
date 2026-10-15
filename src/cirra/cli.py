"""Local review and inspection commands; live collection and publication come later."""

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
from cirra.store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Cirra advisory review coordination (offline prototype)"
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("review", help="review a local fixture and refresh its CI evidence")
    run.add_argument("--fixture", type=Path, required=True)
    run.add_argument("--config", type=Path, required=True, help="explicit trusted operator policy")
    run.add_argument("--state-dir", type=Path, default=Path(".cirra"))
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
        if args.command == "review":
            config = load_config(args.config)
            provider = FixtureProvider(args.fixture, config)
            report, reused = review(provider, config, Store(args.state_dir), rereview=args.rereview)
            paths = export_report(report, args.state_dir)
            print(
                json.dumps(
                    {
                        "run_id": report["run_id"],
                        "status": report["status"],
                        "reused": reused,
                        **paths,
                    },
                    indent=2,
                )
            )
            return 0 if report["status"] == "complete" else 1
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
