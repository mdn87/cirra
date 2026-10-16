"""Supervise an isolated checkout/test run and attest native or standalone evidence."""

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from cirra import provenance
from cirra.domain import CirraError, canonical, digest, fields, number, string, timestamp, utc_now
from cirra.github import API_VERSION, check_from, record
from cirra.locking import change_lock
from cirra.observer_process import run_control
from cirra.observer_source import export_source
from cirra.publish import write_receipt
from cirra.service_config import read_yaml
from cirra.transport import JSONClient, RemoteUncertain, pages

NAME = "cirra/isolated-tests"


def load_observer(path):
    data, _ = read_yaml(path)
    fields(
        data,
        ["version", "image", "commands"],
        ["timeout_seconds", "memory_mb", "cpus", "docker_context"],
        label="observer policy",
    )
    if type(data["version"]) is not int or data["version"] != 1:
        raise CirraError("unsupported observer policy version")
    if not isinstance(data["image"], str) or not re.fullmatch(
        r"sha256:[a-f0-9]{64}", data["image"]
    ):
        raise CirraError("observer image must be a locally installed immutable sha256 image ID")
    commands = data["commands"]
    if not isinstance(commands, list) or not 1 <= len(commands) <= 20:
        raise CirraError("observer policy needs one to 20 explicit argument arrays")
    for command in commands:
        if not isinstance(command, list) or not 1 <= len(command) <= 64:
            raise CirraError("observer commands must be nonempty argument arrays")
        for arg in command:
            string(arg, "observer command argument", 4096)
    data = {
        "timeout_seconds": 600,
        "memory_mb": 1024,
        "cpus": 2,
        "docker_context": "desktop-linux" if os.name == "nt" else "default",
        **data,
    }
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", str(data["docker_context"])):
        raise CirraError("observer Docker context must be an explicit local context name")
    number(data["timeout_seconds"], "observer timeout", 1, 1800, integral=True)
    number(data["memory_mb"], "observer memory MiB", 128, 8192, integral=True)
    number(data["cpus"], "observer CPUs", 0.1, 8)
    data["launcher_sha256"] = hashlib.sha256(
        Path(__file__).with_name("observer_runner.py").read_bytes()
    ).hexdigest()
    data["supervisor_digest"] = digest(
        {
            name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in (
                "observer.py",
                "observer_source.py",
                "observer_process.py",
                "provenance.py",
            )
        }
    )
    data["definition_digest"] = digest({"protocol": "cirra.docker-observer.v1", **data})
    return data


class DockerExecutor:
    def command(self, policy, directory):
        prefix = ["docker", "--context", policy["docker_context"]]
        endpoint = json.loads(
            run_control(
                [
                    *prefix,
                    "context",
                    "inspect",
                    policy["docker_context"],
                    "--format",
                    "{{json .Endpoints.docker.Host}}",
                ],
                directory,
            )
        )
        if not isinstance(endpoint, str) or not endpoint.startswith(("unix://", "npipe://")):
            raise CirraError("observer requires a local Docker socket, not a remote daemon")

        def run(args, **kwargs):
            return run_control([*prefix, *args], directory, **kwargs)

        return run

    def recover(self, policy, saved, directory):
        name = saved["container_name"]
        if not re.fullmatch(r"cirra-observer-[a-f0-9]{32}", name):
            raise CirraError("interrupted observer has an invalid container name")
        run = self.command(policy, directory)
        names = (
            run(["ps", "--all", "--filter", "name=^/" + name + "$", "--format", "{{.Names}}"])
            .decode()
            .splitlines()
        )
        if names and names != [name]:
            raise CirraError("interrupted observer container identity is ambiguous")
        if names:
            run(["rm", "--force", name])

    def run(self, policy, archive, directory, name):
        run = self.command(policy, directory)
        info = json.loads(run(["info", "--format", "{{json .}}"]))
        if info.get("OSType") != "linux":
            raise CirraError("the observer requires a Linux Docker engine")
        image = json.loads(run(["image", "inspect", policy["image"]]))[0]
        if image.get("Id") != policy["image"] or image.get("Os") != "linux":
            raise CirraError("Docker image does not match the pinned Linux image")
        if image.get("Config", {}).get("Volumes"):
            raise CirraError("observer images must not declare persistent volumes")
        inputs = directory / "input"
        inputs.mkdir()
        # The service uses umask 0077 for credentials and state. Only these
        # credential-free inputs must be readable by the sandbox's different UID.
        inputs.chmod(0o755)
        shutil.copyfile(archive, inputs / "source.tar")
        shutil.copyfile(Path(__file__).with_name("observer_runner.py"), inputs / "runner.py")
        if (
            hashlib.sha256((inputs / "runner.py").read_bytes()).hexdigest()
            != policy["launcher_sha256"]
        ):
            raise CirraError(
                "observer launcher changed after policy validation; restart the worker"
            )
        (inputs / "commands.json").write_text(canonical(policy["commands"]), encoding="utf-8")
        for item in inputs.iterdir():
            item.chmod(0o644)
        # Docker bind mounts are local to the selected daemon. Never mount the
        # project root, state directory, credentials, or the Docker socket.
        if "," in str(inputs):
            raise CirraError("observer state path cannot contain commas")
        args = [
            "create",
            "--name",
            name,
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            "--pids-limit=128",
            f"--memory={policy['memory_mb']}m",
            f"--memory-swap={policy['memory_mb']}m",
            f"--cpus={policy['cpus']}",
            "--tmpfs=/work:rw,nosuid,nodev,size=512m,mode=1777",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777",
            "--log-driver=json-file",
            "--log-opt=max-size=1m",
            "--log-opt=max-file=1",
            "--mount",
            f"type=bind,src={inputs},dst=/input,readonly",
            "--entrypoint=/usr/local/bin/python",
            policy["image"],
            "-I",
            "/input/runner.py",
        ]
        container = None
        try:
            container = run(args).decode().strip()
            if not re.fullmatch(r"[a-f0-9]{64}", container):
                raise CirraError("Docker did not return a valid container identity")
            run(["start", container])
            result = run(["wait", container], timeout=policy["timeout_seconds"]).decode().strip()
            state = json.loads(run(["inspect", container]))[0]
            if state.get("Id") != container or state["State"]["Running"]:
                raise CirraError("Docker did not establish a terminal container state")
            exit_code = int(result)
            if state["State"]["ExitCode"] != exit_code or state["State"].get("OOMKilled"):
                raise CirraError("container state is inconsistent or exceeded its memory limit")
            try:
                log = run(["logs", "--tail", "200", container], maximum=131072)
            except CirraError:
                log = b"Container log unavailable or exceeded the 128 KiB limit.\n"
            (directory / "container.log").write_bytes(log)
            return {
                "container_id": container,
                "image_id": policy["image"],
                "exit_code": exit_code,
                "log_path": str(directory / "container.log"),
                "log_sha256": hashlib.sha256(log).hexdigest(),
            }
        finally:
            # The name was generated and recorded before create, so even a lost
            # create acknowledgement cannot leave a running sandbox after errors.
            run(["rm", "--force", container or name])


class ObserverCheck:
    def __init__(self, repository, app_id, token, *, http=None):
        self.repository, self.app_id = repository, str(app_id)
        self.token = token
        self.http, self.headers = (
            http,
            {
                "X-GitHub-Api-Version": API_VERSION,
            },
        )

    def request(self, *args, **kwargs):
        # Execution can take minutes. Start transport deadlines after execution,
        # not when the observer publisher is constructed.
        if "Authorization" not in self.headers:
            token = self.token() if callable(self.token) else self.token
            self.headers["Authorization"] = "Bearer " + token
        return (self.http or JSONClient("https://api.github.com", self.headers)).request(
            *args, **kwargs
        )

    def validate(self, item, head, identifier, result):
        if (
            record(item).get("head_sha") != head
            or item.get("external_id") != identifier
            or item.get("name") != NAME
            or str(record(item.get("app")).get("id")) != self.app_id
            or item.get("status") != "completed"
            or item.get("conclusion") != result
        ):
            raise RemoteUncertain("observer check acknowledgement does not match its execution")
        check_from(item, "check_run", head, utc_now())
        return item

    def find(self, head, identifier, result):
        def get(route, params):
            data, more = self.request("GET", route, params)
            return record(data).get("check_runs"), more

        items = pages(
            get,
            f"/repos/{self.repository}/commits/{head}/check-runs",
            params={"filter": "all", "check_name": NAME},
        )
        return [
            self.validate(item, head, identifier, result)
            for item in items
            if item.get("external_id") == identifier
            and str(record(item.get("app")).get("id")) == self.app_id
        ]

    def create(self, head, identifier, result, started_at, definition):
        item, _ = self.request(
            "POST",
            f"/repos/{self.repository}/check-runs",
            body={
                "name": NAME,
                "head_sha": head,
                "external_id": identifier,
                "status": "completed",
                "conclusion": result,
                "started_at": started_at,
                "completed_at": utc_now(),
                "output": {
                    "title": "Independently observed container tests",
                    "summary": f"Tested commit `{head}` in an isolated Linux container.\n\n"
                    f"Operator execution definition: `{definition}`.\n\n"
                    "The signed checkout receipt is retained by the observer.",
                },
            },
        )
        return self.validate(item, head, identifier, result)


def observe(
    data,
    policy,
    state_dir,
    key,
    publisher=None,
    *,
    token=None,
    executor=None,
    source=None,
    on_execution=None,
):
    change = data["change"]
    provider = change["provider"]
    if provider not in ("github", "gitlab") or change["instance"] != f"https://{provider}.com":
        raise CirraError("independent Docker observation supports GitHub and GitLab")
    if (provider == "github") != (publisher is not None):
        raise CirraError("only GitHub observation uses a native check publisher")
    provenance.sign(
        {"version": 1, "witness": "validation", "observed_at": utc_now(), "checks": []}, key
    )
    identity = {
        "repository": {k: change[k] for k in ("provider", "instance", "repository_id")},
        "head": change["head_sha"],
        "definition": policy["definition_digest"],
        **({"app_id": publisher.app_id} if publisher else {"source_id": "cirra/docker"}),
    }
    identifier = digest(identity)
    root = Path(state_dir).resolve() / "observer"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (identifier + ".json")
    receipt_path = root / (identifier + "-witness.json")
    with change_lock(root / "locks", identifier, 0):
        saved = json.loads(path.read_text()) if path.exists() else None
        if saved and saved["identity"] != identity:
            raise CirraError("saved observation does not match this checkout or execution policy")
        if saved and saved["state"] == "running":
            # The sandbox has no network or shared writable state. Remove the
            # exact recorded sandbox, then execute a new attempt; never attest
            # the unknown attempt. No model call is repeated here.
            (executor or DockerExecutor()).recover(policy, saved, root)
            write_receipt(path, {**saved, "state": "interrupted"})
            saved = None
        if saved and saved["state"] == "interrupted":
            saved = None
        if not saved:
            directory = root / (identifier + "-" + uuid.uuid4().hex)
            branch = data["collection"].get("branch")
            archive, parents = (source or export_source)(
                data["collection"]["repository"],
                None if branch else int(change["change_id"]),
                change["head_sha"],
                directory,
                token,
                provider=provider,
                **({"branch": branch} if branch else {}),
            )
            name = "cirra-observer-" + uuid.uuid4().hex
            saved = {
                "state": "running",
                "identity": identity,
                "container_name": name,
                "started_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
                "parents": parents,
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
            write_receipt(path, saved)
            execution = (executor or DockerExecutor()).run(policy, archive, directory, name)
            saved.update(state="executed", execution=execution)
            write_receipt(path, saved)
        result = "success" if saved["execution"]["exit_code"] == 0 else "failure"
        # Preserve signed execution evidence BEFORE any provider write. A Checks
        # outage must not erase a completed test result or require reexecution.
        standalone = {
            "kind": "observer_run",
            "source_id": "cirra/docker",
            "name": NAME,
            "native_id": saved["execution"]["container_id"],
            "run_id": identifier,
            "attempt": None,
            "raw_state": result,
            "normalized_state": result,
            "associated_sha": change["head_sha"],
            "provenance": {"trusted": False, "tested_sha": None, "parents": []},
            "trigger": "independent_observer",
            "url": f"https://{provider}.com/{data['collection']['repository']}/"
            + ("pull/" if provider == "github" else "-/merge_requests/")
            + change["change_id"],
            "observed_at": utc_now(),
            "started_at": saved["started_at"],
        }
        if data["collection"].get("branch"):
            standalone["url"] = (
                f"https://{provider}.com/{data['collection']['repository']}/"
                + ("commit/" if provider == "github" else "-/commit/")
                + change["head_sha"]
            )
        local_receipt = root / (identifier + "-execution-witness.json")
        write_receipt(
            local_receipt,
            provenance.sign(
                {
                    "version": 2,
                    "change": change,
                    "witness": "Cirra independent Docker supervisor",
                    "observed_at": utc_now(),
                    "checks": [
                        {
                            "check": standalone,
                            "binding": provenance.binding(change, standalone),
                            "tested_sha": change["head_sha"],
                            "parents": saved["parents"],
                            "definition_digest": policy["definition_digest"],
                            "evidence": "Docker execution "
                            + saved["execution"]["container_id"]
                            + "; source archive SHA-256 "
                            + saved["archive_sha256"],
                        }
                    ],
                },
                key,
            ),
        )
        if on_execution:
            on_execution(local_receipt)
        if provider == "gitlab":
            # GitLab commit-status writes can create pipelines and affect merge
            # eligibility. Retain our own signed execution instead; the maintained
            # advisory MR note is the only remote publication.
            check = standalone
        else:
            check = publish_observation(publisher, change, identifier, result, saved, path, policy)
        statement = {
            "version": 2 if provider == "gitlab" else 1,
            **({"change": change} if provider == "gitlab" else {}),
            "witness": "Cirra independent Docker supervisor",
            "observed_at": utc_now(),
            "checks": [
                {
                    **({"check": check} if provider == "gitlab" else {}),
                    "binding": provenance.binding(change, check),
                    "tested_sha": change["head_sha"],
                    "parents": saved["parents"],
                    "definition_digest": policy["definition_digest"],
                    "evidence": "Docker execution "
                    + saved["execution"]["container_id"]
                    + "; source archive SHA-256 "
                    + saved["archive_sha256"],
                }
            ],
        }
        write_receipt(receipt_path, provenance.sign(statement, key))
        saved.update(
            state="observed" if provider == "gitlab" else "published",
            native_id=check["native_id"],
            receipt_path=str(receipt_path),
        )
        write_receipt(path, saved)
        return receipt_path


def publish_observation(publisher, change, identifier, result, saved, path, policy):
    matches = publisher.find(change["head_sha"], identifier, result)
    if len(matches) > 1:
        raise CirraError(
            "multiple native observer checks match this execution; inspect before proceeding"
        )
    if matches:
        native = matches[0]
    elif saved["state"] in ("publishing", "published"):
        raise CirraError(
            "observer publication is uncertain; reconciliation found no unique native check"
        )
    else:
        saved["state"] = "publishing"
        write_receipt(path, saved)
        native = publisher.create(
            change["head_sha"],
            identifier,
            result,
            saved["started_at"],
            policy["definition_digest"],
        )
    check = check_from(native, "check_run", change["head_sha"], utc_now())
    if check["started_at"] != timestamp(saved["started_at"]):
        raise CirraError("native observer check belongs to another execution attempt")
    return check
