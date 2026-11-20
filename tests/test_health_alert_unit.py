import json
import shlex
from configparser import ConfigParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from cirra import health_notify


@pytest.fixture
def alert_unit():
    unit = Path(__file__).resolve().parents[1] / 'examples/systemd/cirra-health-alert.service'
    config = ConfigParser(interpolation=None)
    config.read(unit, encoding='utf-8')
    assert config['Service']['LoadCredentialEncrypted'].startswith('glas-bws-token:')
    command = shlex.split(config['Service']['ExecStart'])
    assert command[1].endswith('/host-runtime.py')
    assert command[2].endswith('/cirra-health.json')
    return config


@pytest.mark.parametrize('value', [None, ''])
def test_missing_credential_fails_without_marking_or_sending(
    alert_unit, monkeypatch, tmp_path, value
):
    if value is None:
        monkeypatch.delenv('LUGOS_EVENT_AUTH_TOKEN', raising=False)
    else:
        monkeypatch.setenv('LUGOS_EVENT_AUTH_TOKEN', value)
    marker = tmp_path / 'health-alert.pending'
    called = []
    monkeypatch.setattr(health_notify, 'receiver', lambda *_: called.append(True))
    with pytest.raises(ValueError, match='LUGOS_EVENT_AUTH_TOKEN'):
        health_notify.notify('alert', marker)
    assert not marker.exists()
    assert not called


def test_configured_alert_posts_the_marked_incident(alert_unit, monkeypatch, tmp_path):
    monkeypatch.setenv('LUGOS_EVENT_AUTH_TOKEN', 'synthetic-test-token')
    monkeypatch.setattr(health_notify, 'journal_detail', lambda: 'failure')
    marker = tmp_path / 'health-alert.pending'
    sent = []

    class Connection:
        def request(self, method, path, body, headers):
            sent.append((method, path, json.loads(body), headers, marker.exists()))

        def getresponse(self):
            return SimpleNamespace(status=202)

        def close(self):
            pass

    monkeypatch.setattr(health_notify, 'receiver', lambda *_: Connection())
    health_notify.notify('alert', marker)
    method, path, event, headers, marked_before_send = sent[0]
    assert (method, path) == ('POST', '/events')
    assert marked_before_send
    assert headers['Authorization'] == 'Bearer synthetic-test-token'
    assert event['type'] == 'operator_attention.requested'
    assert event['dedupe_key'] == 'cirra-health-lugos-host'
    assert marker.read_text().strip() == event['id']
