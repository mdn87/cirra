"""CLI entry points for evaluations, webhook hints and native workflow requests."""

import json
from pathlib import Path

from cirra import events, release
from cirra.config import load_config
from cirra.domain import CirraError
from cirra.evaluation import evaluate
from cirra.fixture import FixtureProvider
from cirra.github import API_VERSION, export_capture
from cirra.publish import github_app_token
from cirra.store import Store
from cirra.transport import JSONClient


def register(commands):
    evaluation = commands.add_parser("evaluate", help="run a frozen real-reviewer evaluation")
    evaluation.add_argument("--corpus", type=Path, default=Path("eval/cases.json"))
    evaluation.add_argument("--rounds", type=int, default=2)
    evaluation.add_argument("--config", type=Path, required=True)
    evaluation.add_argument("--state-dir", type=Path, default=Path(".cirra"))
    event = commands.add_parser(
        "ingest-event", help="authenticate a webhook and capture current state"
    )
    event.add_argument("--provider", choices=("github", "gitlab", "forgejo"), required=True)
    event.add_argument("--instance", help="explicit Forgejo HTTPS origin")
    event.add_argument("--headers", type=Path, required=True)
    event.add_argument("--body", type=Path, required=True)
    event.add_argument("--secret-env", required=True)
    event.add_argument("--reader-token-env")
    event.add_argument("--config", type=Path, required=True)
    event.add_argument("--state-dir", type=Path, default=Path(".cirra"))
    candidate = commands.add_parser("plan-release", help="save an evidence-bound workflow request")
    candidate.add_argument("--run", required=True)
    candidate.add_argument("--workflow-id", type=int, required=True)
    candidate.add_argument("--ref", required=True)
    candidate.add_argument("--output", type=Path, required=True)
    candidate.add_argument("--state-dir", type=Path, default=Path(".cirra"))
    dispatch = commands.add_parser("dispatch-release", help="request an existing GitHub workflow")
    dispatch.add_argument("--request", type=Path, required=True)
    dispatch.add_argument("--config", type=Path, required=True)
    dispatch.add_argument("--state-dir", type=Path, default=Path(".cirra"))
    dispatch.add_argument("--reader-token-env")
    dispatch.add_argument("--app-id", type=int, required=True)
    dispatch.add_argument("--installation-id", type=int, required=True)
    dispatch.add_argument("--app-key-env", required=True)
    dispatch.add_argument("--provenance", type=Path)
    dispatch.add_argument("--witness-key-env")
    dispatch.add_argument("--reconcile", action="store_true")
    dispatch.add_argument("--retry-uncertain", action="store_true")


def read_json(path, maximum=1024 * 1024):
    with Path(path).open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise CirraError("input JSON exceeds byte limit")
    try:
        return json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise CirraError("input is not valid JSON") from None


def run(args, token_from, capture_change):
    if args.command == "evaluate":
        result = evaluate(args.corpus, load_config(args.config), args.state_dir, rounds=args.rounds)
        print(json.dumps(result, indent=2))
        return 0 if all(item["target_met"] for item in result["rounds"]) else 1
    if args.command == "ingest-event":
        config = load_config(args.config)
        headers = read_json(args.headers, 16384)
        with args.body.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        instance = {"github": "https://github.com", "gitlab": "https://gitlab.com"}.get(
            args.provider, args.instance
        )
        hint = events.event_hint(
            args.provider, instance, raw, headers, token_from(args.secret_env), config
        )

        def capture():
            data = capture_change(
                args.provider,
                hint["repository"],
                hint["change"],
                config,
                token=token_from(args.reader_token_env),
                instance=instance,
            )
            return export_capture(data, args.state_dir)

        result = events.ingest(Store(args.state_dir), hint, capture)
        print(json.dumps(result, indent=2))
        return 0
    if args.command == "plan-release":
        report = Store(args.state_dir).latest_report(args.run)
        request = release.plan(report, args.workflow_id, args.ref)
        args.output.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {"request_path": str(args.output.resolve()), "request_id": request["request_id"]}
            )
        )
        return 0
    if args.command == "dispatch-release":
        request, config = read_json(args.request), load_config(args.config)
        token = github_app_token(
            args.app_id,
            args.installation_id,
            token_from(args.app_key_env),
            request["snapshot"]["repository_id"],
            capability="actions",
        )
        http = JSONClient(
            "https://api.github.com",
            {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": API_VERSION},
        )

        def capture():
            data = capture_change(
                "github",
                request["repository"],
                int(request["snapshot"]["change_id"]),
                config,
                token=token_from(args.reader_token_env),
                witness=args.provenance,
                witness_key=token_from(args.witness_key_env),
            )
            return FixtureProvider.from_data(data, config)

        result = release.dispatch(
            request,
            Store(args.state_dir),
            config,
            capture,
            http,
            reader_http=JSONClient(
                "https://api.github.com",
                {
                    "X-GitHub-Api-Version": API_VERSION,
                    **(
                        {"Authorization": "Bearer " + token_from(args.reader_token_env)}
                        if args.reader_token_env
                        else {}
                    ),
                },
            ),
            reconcile=args.reconcile,
            retry_uncertain=args.retry_uncertain,
        )
        print(json.dumps(result, indent=2))
        return 0 if result["state"] in ("accepted", "observed") else 1
    return None
