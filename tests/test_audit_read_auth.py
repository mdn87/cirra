"""Private read routes require a separate local credential; health remains public."""

import json
import os
import socket
import threading
from http.client import HTTPConnection, HTTPResponse

import pytest

from cirra.audit_reads import AuditReadServer
from cirra.domain import CirraError
from cirra.store import Store

TOKEN = "reader-test-token-" + "a" * 48


@pytest.fixture
def reader(tmp_path, monkeypatch):
    monkeypatch.delenv("CIRRA_AUDIT_READ_TOKEN_FILE", raising=False)
    monkeypatch.setenv("CIRRA_AUDIT_READ_TOKEN", TOKEN)
    Store(tmp_path)
    with AuditReadServer(("127.0.0.1", 0), tmp_path) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(5)


def fetch(server, path, authorization=None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        headers = {} if authorization is None else {"Authorization": authorization}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.parametrize("path", ["/audits", "/audits/" + "a" * 32, "/gates/" + "b" * 64])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic " + TOKEN])
def test_unauthorized_requests_cannot_read_private_records(reader, path, authorization):
    status, headers, body = fetch(reader, path, authorization)
    assert status == 401
    assert headers["WWW-Authenticate"] == 'Bearer realm="cirra-audit-reads"'
    assert body == {"error": "unauthorized"}
    assert headers["Cache-Control"] == "no-store"


def test_token_authorizes_reads_without_changing_the_response_contract(reader):
    status, _, body = fetch(reader, "/audits", "Bearer " + TOKEN)
    assert status == 200
    assert body["record"] == {"audits": [], "next": None}
    assert body["protocol"] == "cirra.audit-reads.v1"
    assert fetch(reader, "/audits", "bearer " + TOKEN)[0] == 200


def test_health_probe_does_not_need_a_credential(reader):
    status, _, body = fetch(reader, "/healthz")
    assert status == 200 and body["record"]["status"] == "ok"
    assert fetch(reader, "/healthz", "Bearer wrong")[0] == 200


def test_token_in_query_is_not_accepted_or_logged(reader, capsys):
    status, _, body = fetch(reader, "/audits?token=" + TOKEN)
    assert status == 401 and body == {"error": "unauthorized"}
    assert TOKEN not in capsys.readouterr().err


def test_duplicate_authorization_headers_are_refused(reader):
    with socket.create_connection(reader.server_address, timeout=5) as client:
        client.sendall(
            (
                "GET /audits HTTP/1.0\r\nAuthorization: Bearer "
                + TOKEN
                + "\r\nAuthorization: Bearer "
                + TOKEN
                + "\r\n\r\n"
            ).encode()
        )
        with HTTPResponse(client) as response:
            response.begin()
            assert response.status == 401


@pytest.mark.parametrize("token", [None, "", "short", "a" * 257, "a" * 32 + "\n"])
def test_startup_refuses_missing_or_invalid_token(tmp_path, monkeypatch, token):
    monkeypatch.delenv("CIRRA_AUDIT_READ_TOKEN_FILE", raising=False)
    monkeypatch.delenv("CIRRA_AUDIT_READ_TOKEN", raising=False)
    if token is not None:
        monkeypatch.setenv("CIRRA_AUDIT_READ_TOKEN", token)
    with pytest.raises(CirraError, match="audit read.*credential") as exc:
        AuditReadServer(("127.0.0.1", 0), tmp_path)
    assert token is None or not token or token not in str(exc.value)


def test_secure_token_file_is_supported_and_not_reread_per_request(tmp_path, monkeypatch):
    monkeypatch.delenv("CIRRA_AUDIT_READ_TOKEN", raising=False)
    token_file = tmp_path / "reader-token"
    token_file.write_text(TOKEN + "\n", encoding="ascii")
    token_file.chmod(0o600)
    monkeypatch.setenv("CIRRA_AUDIT_READ_TOKEN_FILE", str(token_file))
    Store(tmp_path / "state")
    with AuditReadServer(("127.0.0.1", 0), tmp_path / "state") as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            token_file.write_text("changed", encoding="ascii")
            assert fetch(server, "/audits", "Bearer " + TOKEN)[0] == 200
        finally:
            server.shutdown()
            thread.join(5)


@pytest.mark.parametrize("kind", ["missing", "oversized", "symlink", "ambiguous"])
def test_invalid_token_file_configuration_is_refused(tmp_path, monkeypatch, kind):
    monkeypatch.delenv("CIRRA_AUDIT_READ_TOKEN", raising=False)
    path = tmp_path / "credential"
    if kind != "missing":
        path.write_text("a" * 4096 if kind == "oversized" else TOKEN, encoding="ascii")
        path.chmod(0o600)
    if kind == "symlink":
        link = tmp_path / "link"
        try:
            link.symlink_to(path)
        except OSError:
            pytest.skip("file symlinks are unavailable")
        path = link
    if kind == "ambiguous":
        monkeypatch.setenv("CIRRA_AUDIT_READ_TOKEN", TOKEN)
    monkeypatch.setenv("CIRRA_AUDIT_READ_TOKEN_FILE", str(path))
    with pytest.raises(CirraError, match="audit read.*credential") as exc:
        AuditReadServer(("127.0.0.1", 0), tmp_path)
    assert TOKEN not in str(exc.value) and str(path) not in str(exc.value)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permission boundary")
@pytest.mark.parametrize("mode", [0o644, 0o660, 0o700])
def test_token_file_refuses_public_read_or_group_write(tmp_path, monkeypatch, mode):
    monkeypatch.delenv("CIRRA_AUDIT_READ_TOKEN", raising=False)
    path = tmp_path / "credential"
    path.write_text(TOKEN, encoding="ascii")
    path.chmod(mode)
    monkeypatch.setenv("CIRRA_AUDIT_READ_TOKEN_FILE", str(path))
    with pytest.raises(CirraError, match="audit read.*credential"):
        AuditReadServer(("127.0.0.1", 0), tmp_path)
