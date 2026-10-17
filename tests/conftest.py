import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from audit_fixtures import Repository, audit_policy, lines

from cirra.audit_worker import AuditRunner
from cirra.config import load_config
from cirra.fixture import FixtureProvider

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fixture_data():
    return json.loads((ROOT / "examples/change.json").read_text(encoding="utf-8"))


@pytest.fixture
def policy_data():
    return yaml.safe_load((ROOT / "examples/policy.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def inputs(tmp_path, fixture_data, policy_data):
    def write(data=None, policy=None):
        config_path = tmp_path / "policy.yaml"
        fixture_path = tmp_path / "change.json"
        config_path.write_text(yaml.safe_dump(policy or policy_data), encoding="utf-8")
        fixture_path.write_text(json.dumps(data or fixture_data), encoding="utf-8")
        config = load_config(config_path)
        return FixtureProvider(fixture_path, config), config

    return write


@pytest.fixture
def world(tmp_path, policy_data):
    origin = Repository(tmp_path / "origin")
    base = origin.commit({"app.py": lines(20), "pyproject.toml": "[project]\n"})
    origin.git("switch", "-c", "feature")
    head = origin.commit({"app.py": lines(10) + lines(12, "changed")})
    origin.git("update-ref", "refs/pull/7/head", head)
    origin.git("switch", "main")
    state = tmp_path / "state"

    def make(runner=None, gate="critical", **limits):
        audit = {
            "tiers": ["pr", "release", "full"],
            "gates": {"pr": gate},
            "limits": {"debounce_seconds": 0, **limits},
        }
        if runner:
            audit["runner"] = runner
        config = audit_policy(
            tmp_path, policy_data, audit, f"policy-{len(list(tmp_path.glob('policy-*')))}.yaml"
        )
        route = SimpleNamespace(
            name="main",
            provider="github",
            instance="https://github.com",
            repository="example/repo",
            repository_id=config.repositories[0]["repository_id"],
            policy=config,
            reader_env=None,
            publisher={"app_id": 5},
        )
        runner_object = AuditRunner(
            state,
            {"main": route},
            lambda name: None,
            url_for=lambda _: str(origin.path),
            protocols=("file",),
            poll_seconds=0.05,
            grace_seconds=2,
        )
        return route, runner_object

    return SimpleNamespace(origin=origin, base=base, head=head, state=state, make=make)
