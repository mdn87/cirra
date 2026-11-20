import json
import shlex
import subprocess
from configparser import ConfigParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def alert_unit():
    unit = Path(__file__).resolve().parents[1] / "examples/systemd/cirra-health-alert.service"
    config = ConfigParser(interpolation=None)
    config.read(unit, encoding="utf-8")
    _, flag, program = shlex.split(config["Service"]["ExecStart"])
    assert flag == "-c"

    # Exercise the shipped unit with the host journal, marker and receiver isolated.
    def run():
        exec(compile(program, str(unit), "exec"), {})

    with (
        patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="failure")),
        patch.object(Path, "write_text", autospec=True) as write_marker,
        patch("urllib.request.urlopen", return_value=SimpleNamespace(status=202)) as post,
    ):
        yield run, write_marker, post


@pytest.mark.parametrize("value", [None, ""])
def test_missing_credential_fails_without_marking_or_sending(alert_unit, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("LUGOS_EVENT_AUTH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("LUGOS_EVENT_AUTH_TOKEN", value)
    run, write_marker, post = alert_unit
    with pytest.raises(SystemExit, match="LUGOS_EVENT_AUTH_TOKEN") as error:
        run()
    assert error.value.code
    write_marker.assert_not_called()
    post.assert_not_called()


def test_configured_alert_posts_the_marked_incident(alert_unit, monkeypatch):
    monkeypatch.setenv("LUGOS_EVENT_AUTH_TOKEN", "test-credential")
    run, write_marker, post = alert_unit
    run()
    request = post.call_args.args[0]
    event = json.loads(request.data)
    assert request.get_header("Authorization") == "Bearer test-credential"
    assert event["type"] == "operator_attention.requested"
    assert event["dedupe_key"] == "cirra-health-lugos-host"
    marker, body = write_marker.call_args.args
    assert marker.name == "health-alert.pending"
    assert body.strip() == event["id"]
