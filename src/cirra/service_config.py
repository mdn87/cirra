"""Explicit operator-owned routes and credential references for the local services."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from cirra.config import UniqueLoader, load_config
from cirra.domain import CirraError, digest, fields, number, string
from cirra.github import repository_name


def read_yaml(path):
    raw = Path(path).read_bytes()
    if len(raw) > 65536:
        raise CirraError("service configuration exceeds 64 KiB")
    try:
        return yaml.load(raw.decode("utf-8"), Loader=UniqueLoader), hashlib.sha256(raw).hexdigest()
    except (yaml.YAMLError, UnicodeError, RecursionError):
        raise CirraError("service configuration is not valid safe YAML") from None


def env_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise CirraError("credential references must name environment variables")
    return value


def local_path(root, value):
    return (root / string(value, "configuration path")).resolve()


def branch_name(value):
    # Deliberately narrow operator allowlist; never accept arbitrary refspecs.
    if (
        not isinstance(value, str)
        or len(value) > 200
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
        or any(
            part in ("", ".", "..") or part.startswith(".") or part.endswith((".", ".lock"))
            for part in value.split("/")
        )
        or ".." in value
    ):
        raise CirraError("branch must be a safe short branch name")
    return value


@dataclass(frozen=True)
class Route:
    name: str
    provider: str
    repository: str
    repository_id: str
    policy: object
    secret_env: str
    reader_env: str | None
    publisher: dict
    observer: Path | None
    witness_env: str | None
    digest: str
    review_enabled: bool = True
    branches: tuple[str, ...] = ()

    @property
    def instance(self):
        return {"github": "https://github.com", "gitlab": "https://gitlab.com"}[self.provider]


@dataclass(frozen=True)
class ServiceConfig:
    routes: dict
    poll_seconds: float
    retry_seconds: float
    max_attempts: int
    max_jobs: int
    max_jobs_per_day: int = 30
    max_retry_seconds: float = 3600
    reconcile_seconds: float = 0


def load_service(path):
    path = Path(path).resolve()
    data, _ = read_yaml(path)
    fields(data, ["version", "routes"], ["worker"], label="service configuration")
    if type(data["version"]) is not int or data["version"] != 1:
        raise CirraError("unsupported service configuration version")
    if not isinstance(data["routes"], list) or not 1 <= len(data["routes"]) <= 32:
        raise CirraError("service needs between one and 32 explicit routes")
    worker = fields(
        data.get("worker", {}),
        [],
        [
            "poll_seconds",
            "retry_seconds",
            "max_attempts",
            "max_jobs",
            "max_jobs_per_day",
            "max_retry_seconds",
            "reconcile_seconds",
        ],
        label="worker",
    )
    settings = {
        "poll_seconds": 2,
        "retry_seconds": 30,
        "max_attempts": 3,
        "max_jobs": 10000,
        "max_jobs_per_day": 30,
        "max_retry_seconds": 3600,
        "reconcile_seconds": 0,
    }
    settings.update(worker)
    number(settings["poll_seconds"], "poll seconds", 0.1, 60)
    number(settings["retry_seconds"], "retry seconds", 1, 3600)
    number(settings["max_attempts"], "maximum attempts", 1, 10, integral=True)
    number(settings["max_jobs"], "maximum retained deliveries", 1, 100000, integral=True)
    number(settings["max_jobs_per_day"], "maximum daily job attempts", 1, 1000, integral=True)
    number(settings["max_retry_seconds"], "maximum retry seconds", settings["retry_seconds"], 86400)
    number(settings["reconcile_seconds"], "reconcile seconds", 0, 86400)
    if 0 < settings["reconcile_seconds"] < 60:
        raise CirraError("reconciliation must be disabled or at least 60 seconds apart")
    routes = {}
    for item in data["routes"]:
        fields(
            item,
            [
                "name",
                "provider",
                "repository",
                "repository_id",
                "policy",
                "webhook_secret_env",
                "publisher",
            ],
            ["reader_token_env", "observer", "witness_key_env", "review_enabled", "branches"],
            label="service route",
        )
        name = string(item["name"], "route name", 64)
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name) or name in routes:
            raise CirraError("route names must be unique lowercase slugs")
        provider = item["provider"]
        if provider not in ("github", "gitlab"):
            raise CirraError("the service supports GitHub and GitLab routes")
        repository = string(item["repository"], "repository")
        if provider == "github":
            repository_name(repository)
        elif not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", repository):
            raise CirraError("GitLab repository must be a namespace/project path")
        repository_id = string(item["repository_id"], "repository ID")
        if not re.fullmatch(r"[1-9][0-9]*", repository_id):
            raise CirraError("repository ID must be a positive integer string")
        policy = load_config(local_path(path.parent, item["policy"]))
        identity = {
            "provider": provider,
            "instance": f"https://{provider}.com",
            "repository_id": repository_id,
        }
        review_enabled = item.get("review_enabled", True)
        if type(review_enabled) is not bool:
            raise CirraError("review_enabled must be a boolean")
        if identity not in policy.repositories or (
            review_enabled and policy.reviewer_kind != "claude"
        ):
            raise CirraError("service route needs an allowlisted automatic Claude review policy")
        if not isinstance(item["publisher"], dict):
            raise CirraError("publisher configuration must be an object")
        publisher = dict(item["publisher"])
        if provider == "github":
            fields(
                publisher,
                ["app_id", "installation_id"],
                ["private_key_file", "private_key_env"],
                label="publisher",
            )
            if ("private_key_file" in publisher) == ("private_key_env" in publisher):
                raise CirraError("GitHub publisher requires exactly one private-key source")
            for key in ("app_id", "installation_id"):
                number(publisher[key], key, 1, 2**63 - 1, integral=True)
            if "private_key_env" in publisher:
                env_name(publisher["private_key_env"])
            else:
                publisher["private_key_file"] = str(
                    local_path(path.parent, publisher["private_key_file"])
                )
        else:
            fields(
                publisher, ["principal_id", "token_env", "permission_receipt"], label="publisher"
            )
            number(publisher["principal_id"], "publisher ID", 1, 2**63 - 1, integral=True)
            env_name(publisher["token_env"])
            publisher["permission_receipt"] = str(
                local_path(path.parent, publisher["permission_receipt"])
            )
        observer = local_path(path.parent, item["observer"]) if item.get("observer") else None
        witness_env = env_name(item["witness_key_env"]) if item.get("witness_key_env") else None
        if bool(observer) != bool(witness_env):
            raise CirraError("Docker observation requires a policy and a witness key reference")
        if not review_enabled and not observer:
            raise CirraError("CI-only routes require an observer")
        branches = item.get("branches", [])
        if not isinstance(branches, list) or len(branches) > 10:
            raise CirraError("branches must be a list of at most ten operator-selected branches")
        branches = tuple(branch_name(branch) for branch in branches)
        if len(set(branches)) != len(branches) or (branches and not observer):
            raise CirraError("branch CI requires unique branches and an observer")
        routes[name] = Route(
            name,
            provider,
            repository,
            repository_id,
            policy,
            env_name(item["webhook_secret_env"]),
            env_name(item["reader_token_env"]) if item.get("reader_token_env") else None,
            publisher,
            observer,
            witness_env,
            digest({"route": item, "policy": policy.digest}),
            review_enabled,
            branches,
        )
    return ServiceConfig(routes=routes, **settings)
