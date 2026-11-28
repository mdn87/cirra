"""Service CLI entry points; credentials come from the launched process."""

import json
import signal
import sys
import threading
from pathlib import Path

from cirra.audit_reads import DEFAULT_PORT, ROUTES, AuditReadServer
from cirra.domain import CirraError
from cirra.job_queue import AuditQueue, Queue
from cirra.observer import load_observer
from cirra.service_config import load_service
from cirra.webhook import WebhookServer
from cirra.worker import Processor, run_worker


def register(commands):
    for name, help_text in (
        ("enroll-coordination", "enroll the primary or a restored standby with its quorum"),
        ("coordination-status", "show shared admission and this host's private-state readiness"),
        ("install-standby-snapshot", "install a verified private archive for a data-host transfer"),
        ("refresh-standby-snapshot", "cache the approved donor's latest verified private archive"),
        (
            "freeze-coordination",
            "stop private writes for a verified transfer to the other data host",
        ),
        ("serve-webhooks", "accept authenticated events on loopback HTTP"),
        ("worker", "process durable review jobs until stopped"),
        ("enqueue", "queue current PR/MR CI and optional review without a webhook"),
        ("requeue", "recover an inspected terminal job, retaining its prior record"),
        ("sync", "find missed open PR/MR revisions using provider reads"),
        ("recover-webhooks", "recover signed GitHub deliveries into this host's queue"),
    ):
        parser = commands.add_parser(name, help=help_text)
        parser.add_argument("--service-config", type=Path, required=True)
        parser.add_argument("--state-dir", type=Path, default=Path(".cirra/service"))
        if name == "serve-webhooks":
            parser.add_argument("--port", type=int, default=8787)
        elif name == "enroll-coordination":
            parser.add_argument("--mode", choices=("initialize", "join"), required=True)
            parser.add_argument(
                "--source-state",
                type=Path,
                help="compare a replacement root with its quiesced legacy primary",
            )
        elif name == "freeze-coordination":
            parser.add_argument("--target-node", required=True)
        elif name == "install-standby-snapshot":
            parser.add_argument("--archive", required=True, type=Path)
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
        elif name == "recover-webhooks":
            parser.add_argument("--route", required=True)
            parser.add_argument("--hook-id", type=int, required=True)
            parser.add_argument("--token-env", required=True, help="repository Webhooks read token")
            select = parser.add_mutually_exclusive_group(required=True)
            select.add_argument("--delivery-id", action="append", type=int)
            select.add_argument(
                "--snapshot-at", help="time of restored snapshot; replay retained PR deliveries"
            )
            parser.add_argument("--max-pages", type=int, default=10)
    status = commands.add_parser("jobs", help="inspect durable worker job results")
    status.add_argument("--state-dir", type=Path, default=Path(".cirra/service"))
    cold = commands.add_parser(
        "list-cold-evidence", help="write NUL-separated relative worker evidence paths for archival"
    )
    cold.add_argument("--state-dir", type=Path, required=True)
    cold.add_argument("--older-than-days", type=int, default=30)
    health = commands.add_parser(
        "health", help="exit nonzero when the worker, queue, reconcile, or budget needs attention"
    )
    health.add_argument("--state-dir", type=Path, default=Path(".cirra/service"))
    health.add_argument(
        "--service-config", type=Path, help="include configured audit baseline age warnings"
    )
    health.add_argument("--max-heartbeat-age", type=int, default=1200)
    health.add_argument(
        "--attention-window",
        type=int,
        default=None,
        help="Explicit historical filter in seconds; defaults to all unresolved failures",
    )
    observer = commands.add_parser(
        "observer-definition", help="validate and fingerprint an observer policy"
    )
    observer.add_argument("--config", type=Path, required=True)
    pins = commands.add_parser(
        "observer-pins",
        help="check each route's pinned observer digest; --repin updates stale pins",
    )
    pins.add_argument("--service-config", type=Path, required=True)
    pins.add_argument("--repin", action="store_true")
    reads = commands.add_parser(
        "serve-audit-reads",
        help="serve authenticated, read-only audit GET requests on loopback HTTP",
    )
    reads.add_argument("--state-dir", type=Path, default=Path(".cirra/service"))
    reads.add_argument("--port", type=int, default=DEFAULT_PORT)
    generation = commands.add_parser(
        "audit-generation", help="inspect, install, or remove the optional v3 audit counter"
    )
    generation.add_argument("action", choices=("status", "install", "remove"))
    generation.add_argument("--state-dir", type=Path, required=True)


def run(args, credential, capture):
    if args.command == "audit-generation":
        from cirra.audit_generation import maintain

        print(json.dumps(maintain(args.state_dir, args.action)))
        return 0
    if args.command == "list-cold-evidence":
        from cirra.retention import manifest

        manifest(args.state_dir, args.older_than_days, sys.stdout.buffer)
        return 0
    if args.command == "observer-definition":
        data = load_observer(args.config)
        print(json.dumps({"definition_digest": data["definition_digest"], "image": data["image"]}))
        return 0
    if args.command == "observer-pins":
        from cirra.pins import main as pins

        return pins(args.service_config, args.repin)
    if args.command == "health":
        from cirra.health import main as health

        return health(
            args.state_dir,
            args.max_heartbeat_age,
            args.attention_window,
            service_config=load_service(args.service_config) if args.service_config else None,
        )
    if args.command == "jobs":
        from cirra.health import queue_metrics

        queue = Queue(args.state_dir)
        status = queue.directory.parent / "worker-status.json"
        reconciliation = queue.directory.parent / "reconciliation.json"
        ci = queue.directory.parent / "ci"
        print(
            json.dumps(
                {
                    "jobs": queue.status(),
                    "queue_metrics": queue_metrics(queue),
                    "daily_usage": queue.usage(),
                    "worker": queue.read(status) if status.exists() else None,
                    "reconciliation": queue.read(reconciliation)
                    if reconciliation.exists()
                    else None,
                    "ci": [queue.read(p) for p in sorted(ci.glob("*.json"))],
                    "audits": AuditQueue(args.state_dir).status(),
                },
                indent=2,
            )
        )
        return 0
    if args.command == "serve-audit-reads":
        with AuditReadServer(("127.0.0.1", args.port), args.state_dir) as server:
            print(
                json.dumps(
                    {"listening": f"http://127.0.0.1:{server.server_port}", "routes": list(ROUTES)}
                ),
                flush=True,
            )
            try:
                server.serve_forever(poll_interval=0.5)
            except KeyboardInterrupt:
                pass
        return 0
    if args.command not in (
        "enroll-coordination",
        "coordination-status",
        "install-standby-snapshot",
        "refresh-standby-snapshot",
        "freeze-coordination",
        "serve-webhooks",
        "worker",
        "enqueue",
        "requeue",
        "sync",
        "recover-webhooks",
    ):
        return None
    config = load_service(args.service_config)
    if args.command == "refresh-standby-snapshot":
        from cirra.standby_install import refresh

        print(json.dumps(refresh(config, args.state_dir, credential)))
        return 0
    if args.command == "install-standby-snapshot":
        from cirra.standby_install import install

        print(json.dumps(install(config, args.state_dir, credential, args.archive)))
        return 0
    if args.command in ("coordination-status", "freeze-coordination"):
        from cirra.coordination import coordination_control

        result = coordination_control(
            config, args.state_dir, credential, getattr(args, "target_node", None)
        )
        print(json.dumps(result))
        return 0
    if args.command == "enroll-coordination":
        from cirra.coordination import enroll

        print(
            json.dumps(
                enroll(
                    config,
                    args.state_dir,
                    credential,
                    initialize=args.mode == "initialize",
                    source=args.source_state,
                )
            )
        )
        return 0
    if args.command == "recover-webhooks":
        from cirra.webhook_recovery import recover

        result = recover(config, args, credential)
        print(json.dumps(result))
        return int(bool(result["unresolved"]))
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
    if args.command == "sync":
        from cirra.reconcile import sync

        result = sync(
            config, Queue(args.state_dir, config.max_jobs), credential, route_name=args.route
        )
        print(json.dumps(result))
        return int(any("error" in row or "branch_errors" in row for row in result))
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
