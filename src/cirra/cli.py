"""Local fixture reviews, agent session handoffs, and saved report inspection."""

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from cirra import __version__
from cirra.config import load_config
from cirra.coordinator import review
from cirra.domain import CirraError
from cirra.fixture import FixtureProvider
from cirra.github import GitHubClient, collect, export_capture
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
        run.add_argument("--fixture", "--snapshot", dest="fixture", type=Path, required=True)
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
    capture = commands.add_parser("collect", help="capture a GitHub PR and native CI evidence")
    capture.add_argument("--provider", choices=("github",), required=True)
    capture.add_argument("--repo", required=True, help="GitHub owner/repository")
    capture.add_argument("--change", type=int, required=True, help="pull request number")
    capture.add_argument("--config", type=Path, required=True)
    capture.add_argument("--state-dir", type=Path, default=Path(".cirra"))
    capture.add_argument("--token-env", help="explicit environment variable holding a reader token")
    capture.add_argument(
        "--timeout", type=float, default=60, help="collection time budget in seconds"
    )
    capture.add_argument("--max-pages", type=int, default=10, help="maximum pages per CI endpoint")
    show = commands.add_parser("show", help="show a saved report without invoking a reviewer")
    show.add_argument("--run", required=True)
    show.add_argument("--state-dir", type=Path, default=Path(".cirra"))
    show.add_argument("--format", choices=("markdown", "json"), default="markdown")
    show.add_argument(
        "--export", action="store_true", help="restore JSON/Markdown exports from SQLite"
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            config = load_config(args.config)
            token = None
            if args.token_env:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.token_env):
                    raise CirraError("token-env must name an environment variable")
                token = os.environ.get(args.token_env)
                if not token:
                    raise CirraError("selected GitHub reader token variable is empty or unset")
            client = GitHubClient(args.repo, token=token, timeout=args.timeout)
            data = collect(args.repo, args.change, config, client=client, max_pages=args.max_pages)
            provider = FixtureProvider.from_data(data, config)
            complete = not provider.snapshot.coverage_gaps and not provider.check_errors
            print(
                json.dumps(
                    {
                        "snapshot_path": export_capture(data, args.state_dir),
                        "collection_status": "collected" if complete else "partial",
                        "snapshot": provider.snapshot.record(),
                        "files": len(provider.snapshot.files),
                        "checks": len(provider.checks),
                        "check_errors": provider.check_errors,
                    },
                    indent=2,
                )
            )
            return 0 if complete else 1
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
