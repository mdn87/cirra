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
        ["poll_seconds", "retry_seconds", "max_attempts", "max_jobs", "max_jobs_per_day"],
        label="worker",
    )
    settings = {
        "poll_seconds": 2,
        "retry_seconds": 30,
        "max_attempts": 3,
        "max_jobs": 10000,
        "max_jobs_per_day": 30,
    }
    settings.update(worker)
    number(settings["poll_seconds"], "poll seconds", 0.1, 60)
    number(settings["retry_seconds"], "retry seconds", 1, 3600)
    number(settings["max_attempts"], "maximum attempts", 1, 10, integral=True)
    number(settings["max_jobs"], "maximum retained deliveries", 1, 100000, integral=True)
    number(settings["max_jobs_per_day"], "maximum daily job attempts", 1, 1000, integral=True)
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
            ["reader_token_env", "observer", "witness_key_env"],
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
        if identity not in policy.repositories or policy.reviewer_kind != "claude":
            raise CirraError("service route needs an allowlisted automatic Claude review policy")
        if not isinstance(item["publisher"], dict):
            raise CirraError("publisher configuration must be an object")
        publisher = dict(item["publisher"])
        if provider == "github":
            fields(publisher, ["app_id", "installation_id", "private_key_file"], label="publisher")
            for key in ("app_id", "installation_id"):
                number(publisher[key], key, 1, 2**63 - 1, integral=True)
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
        if bool(observer) != bool(witness_env) or (observer and provider != "github"):
            raise CirraError(
                "Docker observation requires a GitHub route and a witness key reference"
            )
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
        )
    return ServiceConfig(routes=routes, **settings)
