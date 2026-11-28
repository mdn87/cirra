"""Independent host/active heartbeats cannot disclose credentials or hide failure."""

import json
import os
import plistlib
import runpy
import threading
import time
from configparser import ConfigParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from cirra.job_queue import Queue
from cirra.worker import heartbeat

ROOT = Path(__file__).parents[1]
TOKEN = "synthetic-private-token-32-characters"


@pytest.fixture
def reporter():
    return runpy.run_path(str(ROOT / "scripts/report-health.py"))


@pytest.fixture
def kuma():
    control = SimpleNamespace(status=200, body=b'{"ok":true}', paths=[], delay=0)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            control.paths.append(self.path)
            self.send_response(control.status)
            self.send_header("Location", "/redirected/" + TOKEN)
            self.send_header("Content-Length", str(len(control.body)))
            self.end_headers()
            if control.delay:
                time.sleep(control.delay)
            try:
                self.wfile.write(control.body)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        control.url = f"http://127.0.0.1:{server.server_port}/api/push/{TOKEN}"
        try:
            yield control
        finally:
            server.shutdown()
            thread.join(timeout=5)


def credential(path, value):
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def arguments(tmp_path, kuma):
    return SimpleNamespace(
        state_dir=tmp_path,
        service_config=None,
        host_heartbeat_file=credential(tmp_path / "host.url", kuma.url),
        active_heartbeat_file=credential(tmp_path / "active.url", kuma.url + "-active"),
        max_heartbeat_age=1200,
        timeout=1,
    )


@pytest.mark.parametrize("role,expected", [("running", 2), ("standby", 1)])
def test_native_health_reports_host_and_only_current_active_role(
    reporter, kuma, tmp_path, role, expected
):
    args = arguments(tmp_path, kuma)
    heartbeat(Queue(tmp_path), state=role)
    result = reporter["run"](args)
    assert result["healthy"] and result["host_heartbeat"] == "sent"
    assert len(kuma.paths) == expected
    assert all(parse_qs(urlsplit(path).query)["status"] == ["up"] for path in kuma.paths)
    assert result["active_heartbeat"] == ("sent" if role == "running" else "skipped")
    assert TOKEN not in json.dumps(result)


def test_stale_running_status_cannot_refresh_the_active_monitor(reporter, kuma, tmp_path):
    args = arguments(tmp_path, kuma)
    heartbeat(Queue(tmp_path), heartbeat_at="2020-01-01T00:00:00+00:00")
    result = reporter["run"](args)
    assert not result["healthy"] and result["active_heartbeat"] == "skipped"
    assert len(kuma.paths) == 1
    assert parse_qs(urlsplit(kuma.paths[0]).query)["status"] == ["down"]


def test_active_failure_posts_down_to_both_monitors_without_private_details(
    reporter, kuma, tmp_path
):
    args = arguments(tmp_path, kuma)
    queue = Queue(tmp_path)
    receipt = queue.enqueue(
        SimpleNamespace(name="fixture", digest="policy"),
        {"change": 1, "delivery": "fixture", "payload_digest": "fixture"},
    )
    job = queue.review_record(queue.directory / (receipt["job_id"] + ".json"))
    job.update(state="attention", error="private-failure-detail")
    queue.save(job)
    heartbeat(queue)
    result = reporter["run"](args)
    assert not result["healthy"] and result["active_heartbeat"] == "sent"
    assert len(kuma.paths) == 2
    assert all(parse_qs(urlsplit(path).query)["status"] == ["down"] for path in kuma.paths)
    assert "private-failure-detail" not in json.dumps(result) + json.dumps(kuma.paths)


def test_unavailable_health_is_reported_down_without_exception_text(reporter, kuma, tmp_path):
    args = arguments(tmp_path, kuma)

    def broken(_):
        raise RuntimeError(TOKEN)

    reporter["run"].__globals__["evaluate"] = broken
    result = reporter["run"](args)
    assert result["problems"] == ["health_unavailable"]
    assert result["host_heartbeat"] == "sent" and result["active_heartbeat"] == "skipped"
    assert TOKEN not in json.dumps(result)


@pytest.mark.parametrize(
    "status,body", [(302, b""), (500, b"private"), (200, b'{"ok":false}'), (200, b"x" * 4097)]
)
def test_non_acknowledgement_never_redirects_or_echoes_a_response(
    reporter, kuma, tmp_path, status, body
):
    args = arguments(tmp_path, kuma)
    heartbeat(Queue(tmp_path), state="standby")
    kuma.status, kuma.body = status, body
    result = reporter["run"](args)
    assert result["host_heartbeat"] == "failed" and len(kuma.paths) == 1
    assert TOKEN not in json.dumps(result)


def test_timeout_bounds_wait_for_response_body(reporter, kuma, tmp_path):
    args = arguments(tmp_path, kuma)
    heartbeat(Queue(tmp_path), state="standby")
    args.timeout, kuma.delay = 0.1, 1
    started = time.monotonic()
    assert reporter["run"](args)["host_heartbeat"] == "failed"
    assert time.monotonic() - started < 0.8


@pytest.mark.parametrize(
    "url",
    [
        f"http://example.com/api/push/{TOKEN}",
        f"http://8.8.8.8/api/push/{TOKEN}",
        f"ftp://127.0.0.1/api/push/{TOKEN}",
        f"https://user:password@example.com/api/push/{TOKEN}",
        f"https://example.com/api/push/{TOKEN}?status=up",
        f"https://example.com/api/push/{TOKEN}#fragment",
        f"https://example.com/other/{TOKEN}",
        f"https://example.com/api/push/{TOKEN}\r\nInjected: value",
    ],
)
def test_unsafe_credential_destination_is_refused(reporter, tmp_path, url):
    path = credential(tmp_path / "push.url", url)
    with pytest.raises(ValueError):
        reporter["read_push_url"](path)


def test_missing_oversized_and_symlinked_credentials_are_refused(reporter, tmp_path):
    path = tmp_path / "push.url"
    with pytest.raises(OSError):
        reporter["read_push_url"](path)
    credential(path, "x" * 2049)
    with pytest.raises(ValueError, match="size limit"):
        reporter["read_push_url"](path)
    target = credential(tmp_path / "target.url", f"https://example.com/api/push/{TOKEN}")
    link = tmp_path / "linked.url"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation requires an available platform privilege")
    with pytest.raises(ValueError, match="regular"):
        reporter["read_push_url"](link)


@pytest.mark.skipif(os.name != "posix", reason="Unix credential permission bits")
def test_group_readable_credential_is_refused(reporter, tmp_path):
    path = credential(tmp_path / "push.url", f"https://example.com/api/push/{TOKEN}")
    path.chmod(0o640)
    with pytest.raises(ValueError, match="private"):
        reporter["read_push_url"](path)


def test_script_failure_exit_is_sanitized(reporter, kuma, tmp_path, capsys):
    args = arguments(tmp_path, kuma)
    heartbeat(Queue(tmp_path), state="standby")
    kuma.status = 500
    assert (
        reporter["main"](
            [
                "--state-dir",
                str(tmp_path),
                "--host-heartbeat-file",
                str(args.host_heartbeat_file),
                "--active-heartbeat-file",
                str(args.active_heartbeat_file),
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert json.loads(output.out)["host_heartbeat"] == "failed"


def test_site_examples_have_independent_monitoring_and_private_credential_files():
    plist = plistlib.loads((ROOT / "examples/launchd/foo.newman.cirra-health.plist").read_bytes())
    assert plist["StartInterval"] == 300 and plist["Umask"] == 0o077
    argv = plist["ProgramArguments"]
    assert "--host-heartbeat-file" in argv and "--active-heartbeat-file" in argv
    assert any("ha-current/scripts/report-health.py" in arg for arg in argv)
    unit = (ROOT / "examples/systemd/ha/cirra-health.service.d/zzz-kuma-push.conf").read_text()
    assert "LoadCredential=host-heartbeat:" in unit
    assert "--host-heartbeat-file %d/host-heartbeat" in unit
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in unit
    assert "TimeoutStartSec=90" in unit


def test_health_publisher_is_the_effective_command_after_all_ha_dropins():
    base = ROOT / "examples/systemd/cirra-health.service"
    dropins = ROOT / "examples/systemd/ha/cirra-health.service.d"
    config = ConfigParser(interpolation=None, strict=False)
    config.read([base, *sorted(dropins.glob("*.conf"))], encoding="utf-8")
    command = config["Service"]["ExecStart"]
    assert "ha-current/scripts/report-health.py" in command
    assert "AF_INET" in config["Service"]["RestrictAddressFamilies"].split()
