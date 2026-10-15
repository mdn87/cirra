"""Local fixture reviews, agent session handoffs, and saved report inspection."""

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from cirra import __version__, extensions, gitlab, provenance
from cirra.config import load_config
from cirra.coordinator import review
from cirra.domain import CirraError
from cirra.fixture import FixtureProvider
from cirra.github import GitHubClient, collect, export_capture
from cirra.publish import (
    GitHubPublisher,
    GitLabPublisher,
    github_app_token,
    publish,
    verify_gitlab_permission_receipt,
)
from cirra.report import export_report, render_markdown
from cirra.session import export_packet, import_response, prepare
from cirra.store import Store


def token_from(name):
    if not name:
        return None
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise CirraError("token-env must name an environment variable")
    value = os.environ.get(name)
    if not value:
        raise CirraError("selected credential environment variable is empty or unset")
    return value


def capture_change(
    provider,
    repository,
    change,
    config,
    *,
    token=None,
    timeout=60,
    max_pages=10,
    instance=None,
    witness=None,
    witness_key=None,
):
    if provider == "github":
        client = GitHubClient(repository, token=token, timeout=timeout)
        data = collect(repository, change, config, client=client, max_pages=max_pages)
    elif provider == "gitlab":
        client = gitlab.GitLabClient(repository, token=token, timeout=timeout)
        data = gitlab.collect(repository, change, config, client=client, max_pages=max_pages)
    else:
        from cirra import forgejo

        client = forgejo.ForgejoClient(instance, repository, token=token, timeout=timeout)
        data = forgejo.collect(repository, change, config, client=client, max_pages=max_pages)
    if bool(witness) != bool(witness_key):
        raise CirraError("checkout witness receipt and key must be selected together")
    return provenance.apply(data, witness, witness_key) if witness else data


def collector_options(command):
    command.add_argument("--provider", choices=("github", "gitlab", "forgejo"))
    command.add_argument("--repo", help="provider owner/repository or namespace/project")
    command.add_argument("--change", type=int, help="pull or merge request number")
    command.add_argument("--instance", help="explicit Forgejo HTTPS origin")
    command.add_argument("--token-env", help="explicit reader credential environment variable")
    command.add_argument("--timeout", type=float, default=60)
    command.add_argument("--max-pages", type=int, default=10)
    command.add_argument("--provenance", type=Path, help="signed checkout witness receipt")
    command.add_argument("--witness-key-env", help="trusted checkout witness HMAC key variable")


def capture_args(args, config):
    if not args.provider or not args.repo or not args.change:
        raise CirraError("live capture requires --provider, --repo and --change")
    return capture_change(
        args.provider,
        args.repo,
        args.change,
        config,
        token=token_from(args.token_env),
        timeout=args.timeout,
        max_pages=args.max_pages,
        instance=args.instance,
        witness=args.provenance,
        witness_key=token_from(args.witness_key_env),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cirra advisory review coordination")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    extensions.register(commands)
    for name, help_text in (
        ("review", "review a captured change with the configured fake or automatic reviewer"),
        ("prepare-review", "export a packet for an existing agent session"),
        ("import-review", "validate and record an agent session response"),
    ):
        run = commands.add_parser(name, help=help_text)
        run.add_argument(
            "--fixture", "--snapshot", dest="fixture", type=Path, required=name != "review"
        )
        if name == "review":
            collector_options(run)
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
    capture = commands.add_parser("collect", help="capture a native change and CI evidence")
    collector_options(capture)
    capture.add_argument("--config", type=Path, required=True)
    capture.add_argument("--state-dir", type=Path, default=Path(".cirra"))
    publication = commands.add_parser(
        "publish", help="refresh CI and publish an advisory native report"
    )
    publication.add_argument("--run", required=True)
    publication.add_argument("--config", type=Path, required=True)
    publication.add_argument("--state-dir", type=Path, default=Path(".cirra"))
    publication.add_argument("--reader-token-env")
    publication.add_argument("--publisher-token-env")
    publication.add_argument(
        "--publisher-id", type=int, required=True, help="GitHub App ID or GitLab user ID"
    )
    publication.add_argument("--installation-id", type=int, help="GitHub App installation ID")
    publication.add_argument("--app-key-env", help="GitHub App PEM signing key variable")
    publication.add_argument(
        "--permission-receipt", type=Path, help="GitLab operator-verified scope receipt"
    )
    publication.add_argument("--reconcile", action="store_true")
    publication.add_argument("--retry-uncertain", action="store_true")
    publication.add_argument("--canonical-id")
    publication.add_argument("--provenance", type=Path)
    publication.add_argument("--witness-key-env")
    signing = commands.add_parser(
        "sign-provenance", help="sign an independently verified checkout statement"
    )
    signing.add_argument("--statement", type=Path, required=True)
    signing.add_argument("--key-env", required=True)
    signing.add_argument("--output", type=Path, required=True)
    show = commands.add_parser("show", help="show a saved report without invoking a reviewer")
    show.add_argument("--run", required=True)
    show.add_argument("--state-dir", type=Path, default=Path(".cirra"))
    show.add_argument("--format", choices=("markdown", "json"), default="markdown")
    show.add_argument(
        "--export", action="store_true", help="restore JSON/Markdown exports from SQLite"
    )
    args = parser.parse_args(argv)
    try:
        extension_result = extensions.run(args, token_from, capture_change)
        if extension_result is not None:
            return extension_result
        if args.command == "sign-provenance":
            statement = extensions.read_json(args.statement)
            signed = provenance.sign(statement, token_from(args.key_env))
            args.output.write_text(json.dumps(signed, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"receipt_path": str(args.output.resolve())}))
            return 0
        if args.command == "publish":
            config, store = load_config(args.config), Store(args.state_dir)
            saved = store.latest_report(args.run)
            snap = saved["snapshot"]
            if not saved.get("collection"):
                raise CirraError("native publication requires a captured provider change")
            repo = saved["collection"]["repository"]
            reader = token_from(args.reader_token_env)
            if snap["provider"] == "github":
                if not args.app_key_env or not args.installation_id:
                    raise CirraError(
                        "GitHub publication needs --app-key-env and --installation-id "
                        "for a Checks-only installation token"
                    )
                token = github_app_token(
                    args.publisher_id,
                    args.installation_id,
                    token_from(args.app_key_env),
                    snap["repository_id"],
                )
                publisher = GitHubPublisher(repo, args.publisher_id, token)
            elif snap["provider"] == "gitlab":
                token = token_from(args.publisher_token_env)
                if not token or token == reader or not args.permission_receipt:
                    raise CirraError(
                        "GitLab publication needs a separate publisher credential "
                        "and --permission-receipt"
                    )
                verify_gitlab_permission_receipt(
                    args.permission_receipt, token, snap["repository_id"], args.publisher_id
                )
                publisher = GitLabPublisher(
                    gitlab.GitLabClient(repo, token=token), args.publisher_id
                )
            else:
                raise CirraError("Forgejo publication is not an enabled capability")

            def refresh():
                data = capture_change(
                    snap["provider"],
                    repo,
                    int(snap["change_id"]),
                    config,
                    token=reader,
                    witness=args.provenance,
                    witness_key=token_from(args.witness_key_env),
                )
                return FixtureProvider.from_data(data, config)

            receipt = publish(
                store,
                config,
                args.run,
                refresh,
                publisher,
                reconcile=args.reconcile,
                retry_uncertain=args.retry_uncertain,
                canonical_id=args.canonical_id,
            )
            print(json.dumps(receipt, indent=2))
            return 0 if receipt["state"] == "published" else 1
        if args.command == "collect":
            config = load_config(args.config)
            data = capture_args(args, config)
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
            if args.command == "review" and not args.fixture:
                data = capture_args(args, config)
                export_capture(data, args.state_dir)
                provider = FixtureProvider.from_data(data, config)
            else:
                if args.command == "review" and args.provider:
                    raise CirraError("choose either --snapshot or live provider arguments")
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
