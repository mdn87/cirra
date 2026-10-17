"""Service CLI entry points; no implicit environment-file loading or public bind."""

import json
import os
import signal
import threading
from pathlib import Path

from cirra.domain import CirraError
from cirra.job_queue import Queue
from cirra.observer import load_observer
from cirra.service_config import load_service
from cirra.webhook import WebhookServer
from cirra.worker import Processor, run_worker


def register(commands):
    for name, help_text in (
        ("serve-webhooks", "accept authenticated events on loopback HTTP"),
        ("worker", "process durable review jobs until stopped"),
        ("enqueue", "queue current PR/MR CI and optional review without a webhook"),
        ("requeue", "recover an inspected terminal job, retaining its prior record"),
        ("sync", "find missed open PR/MR revisions using provider reads"),
    ):
        parser = commands.add_parser(name, help=help_text)
        parser.add_argument("--service-config", type=Path, required=True)
        parser.add_argument("--state-dir", type=Path, default=Path(".cirra/service"))
        parser.add_argument(
            "--env-file", type=Path, help="explicit file of selected credential values"
        )
        if name == "serve-webhooks":
            parser.add_argument("--port", type=int, default=8787)
        elif name == "worker":
            parser.add_argument("--once", action="store_true")
        elif name == "enqueue":
            parser.add_argument("--route", required=True)
            selection = parser.add_mutually_exclusive_group(required=True)
            selection.add_argument("--change", type=int)
            selection.add_argument("--branch")
        elif name == "requeue":
            parser.add_argument("--route", required=True)
            parser.add_argument("--job", required=True)
            parser.add_argument("--reason", required=True)
        elif name == "sync":
            parser.add_argument("--route")
    status = commands.add_parser("jobs", help="inspect durable worker job results")
    status.add_argument("--state-dir", type=Path, default=Path(".cirra/service"))
    observer = commands.add_parser(
        "observer-definition", help="validate and fingerprint an observer policy"
    )
    observer.add_argument("--config", type=Path, required=True)


def run(args, credential, capture):
    if args.command == "observer-definition":
        data = load_observer(args.config)
        print(json.dumps({"definition_digest": data["definition_digest"], "image": data["image"]}))
        return 0
    if args.command == "jobs":
        queue = Queue(args.state_dir)
        status = queue.directory.parent / "worker-status.json"
        reconciliation = queue.directory.parent / "reconciliation.json"
        ci = queue.directory.parent / "ci"
        print(
            json.dumps(
                {
                    "jobs": queue.status(),
                    "daily_usage": queue.usage(),
                    "worker": queue.read(status) if status.exists() else None,
                    "reconciliation": queue.read(reconciliation)
                    if reconciliation.exists()
                    else None,
                    "ci": [queue.read(p) for p in sorted(ci.glob("*.json"))],
                },
                indent=2,
            )
        )
        return 0
    if args.command not in ("serve-webhooks", "worker", "enqueue", "requeue", "sync"):
        return None
    config = load_service(args.service_config)
    if args.command in ("enqueue", "requeue"):
        route = config.routes.get(args.route)
        if route is None:
            raise CirraError("unknown service route")
        queue = Queue(args.state_dir, config.max_jobs)
        result = (
            (
                queue.request_branch(route, args.branch)
                if args.branch
                else queue.request(route, args.change)
            )
            if args.command == "enqueue"
            else queue.requeue(args.job, route, args.reason)
        )
        print(json.dumps(result))
        return 0
    if args.env_file:
        names = set()
        for route in config.routes.values():
            if args.command == "serve-webhooks":
                names.add(route.secret_env)
            elif args.command == "sync":
                names.add(route.reader_env)
            else:
                names.update(
                    [route.reader_env, route.witness_env, route.publisher.get("token_env")]
                )
        credential = credential_file(args.env_file, names - {None})
    if args.command == "sync":
        from cirra.reconcile import sync

        result = sync(
            config, Queue(args.state_dir, config.max_jobs), credential, route_name=args.route
        )
        print(json.dumps(result))
        return int(any("error" in row for row in result))
    if args.command == "serve-webhooks":
        with WebhookServer(("127.0.0.1", args.port), config, args.state_dir, credential) as server:
            print(
                json.dumps(
                    {
                        "listening": f"http://127.0.0.1:{server.server_port}",
                        "routes": ["/hooks/" + name for name in config.routes],
                    }
                ),
                flush=True,
            )
            try:
                server.serve_forever(poll_interval=0.5)
            except KeyboardInterrupt:
                pass
        return 0
    stopped = threading.Event()
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, lambda *_: stopped.set())
    try:
        result = run_worker(
            config,
            args.state_dir,
            Processor(args.state_dir, credential, capture),
            once=args.once,
            stopped=stopped,
        )
        return int(
            bool(
                result
                and (
                    result["state"] in ("retry", "attention")
                    or (result.get("result", {}).get("ci") or {}).get("conclusion") == "failure"
                )
            )
        )
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def credential_file(path, names):
    """Read only explicitly selected values; never execute shell syntax or expand variables."""
    raw = Path(path).read_bytes()
    if len(raw) > 65536:
        raise CirraError("credential file exceeds 64 KiB")
    values = {}
    for line in raw.decode("utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if key not in names:
            continue
        if not separator or key in values:
            raise CirraError("selected credential file entries must be unique NAME=value lines")
        value = value.strip()
        if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
            value = value[1:-1]
        values[key] = value

    def credential(name):
        if name is None:
            return None
        if name not in names:
            raise CirraError("credential is not selected for this service process")
        value = os.environ.get(name) or values.get(name)
        if not value:
            raise CirraError("selected service credential is empty or unset")
        return value

    return credential
