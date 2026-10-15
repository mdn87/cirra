import json
from pathlib import Path

import pytest
import yaml

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
