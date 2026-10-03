import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from workgate.config.settings import clear_settings_cache
from workgate.control.http.app import build_http_app
from workgate.oauth.http.auth import is_direct_localhost_request
from workgate.tools.catalog import build_tool_catalog


def _auth_request(
    *,
    peer: tuple[str, int] | None,
    host: str | None,
    headers: tuple[tuple[str, str], ...] = (),
) -> Request:
    raw_headers: list[tuple[bytes, bytes]] = []
    if host is not None:
        raw_headers.append((b"host", host.encode("ascii")))
    raw_headers.extend(
        (name.encode("ascii"), value.encode("ascii")) for name, value in headers
    )
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "raw_path": b"/",
            "root_path": "",
            "scheme": "http",
            "query_string": b"",
            "headers": raw_headers,
            "client": peer,
            "server": ("127.0.0.1", 8765),
        }
    )


def test_http_missing_required_argument_returns_validation_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    response = TestClient(build_http_app()).post("/tools/read", json={})

    assert response.status_code == 400
    assert response.json() == {
        "error": "validation_error",
        "message": "Missing required argument: session_id",
    }


def test_http_argument_validation_uses_consistent_error_envelope(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    # Validate an argument before entering the fail-closed session router.
    client = TestClient(build_http_app(tool_catalog=build_tool_catalog()))
    response = client.post(
        "/tools/session_start",
        json={"workdir": ".", "label": ""},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "validation_error"
    assert "at least 1 character" in response.json()["message"]


def test_http_unknown_session_returns_validation_error(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    response = TestClient(build_http_app()).post(
        "/tools/read",
        json={"session_id": "BAD00000", "path": "missing.txt"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "validation_error",
        "message": "unknown session_id 'BAD00000'; call session_start first",
    }


def test_http_app_exposes_oauth_public_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_MODE", "http")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "oauth")
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://example.com")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    client = TestClient(build_http_app())

    server_metadata = client.get("/.well-known/oauth-authorization-server")
    assert server_metadata.status_code == 200
    assert (
        server_metadata.json()["token_endpoint"]
        == "https://example.com/oauth/token"
    )

    resource_metadata = client.get("/.well-known/oauth-protected-resource/mcp")
    assert resource_metadata.status_code == 200
    assert resource_metadata.json()["resource"] == "https://example.com/mcp"


def test_http_localhost_bypass_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_MODE", "http")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "oauth")
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://example.com")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    protected = TestClient(
        build_http_app(),
        base_url="http://127.0.0.1:8765",
        client=("127.0.0.1", 50000),
    ).post("/tools/read", json={})
    assert protected.status_code == 401

    monkeypatch.setenv("WORKGATE_AUTH_BYPASS_LOCALHOST", "true")
    clear_settings_cache()
    bypassed = TestClient(
        build_http_app(),
        base_url="http://127.0.0.1:8765",
        client=("127.0.0.1", 50000),
    ).post("/tools/read", json={})
    assert bypassed.status_code == 400
    assert bypassed.json()["error"] == "validation_error"


@pytest.mark.parametrize(
    ("base_url", "peer", "headers"),
    (
        ("http://127.0.0.1:8765", ("127.0.0.1", 50000), {}),
        (
            "http://localhost:8765",
            ("::1", 50000),
            {"Host": "[::1]:8765"},
        ),
    ),
)
def test_http_localhost_bypass_accepts_direct_loopback(
    tmp_path, monkeypatch, base_url, peer, headers
):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_MODE", "http")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "oauth")
    monkeypatch.setenv("WORKGATE_AUTH_BYPASS_LOCALHOST", "true")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    response = TestClient(
        build_http_app(), base_url=base_url, client=peer
    ).post("/tools/read", json={}, headers=headers)

    assert response.status_code == 400
    assert response.json()["error"] == "validation_error"


@pytest.mark.parametrize(
    ("base_url", "peer", "headers"),
    (
        ("http://workgate.example", ("127.0.0.1", 50000), {}),
        ("http://127.0.0.1:8765", ("203.0.113.10", 50000), {}),
        (
            "http://127.0.0.1:8765",
            ("127.0.0.1", 50000),
            {"Forwarded": "for=203.0.113.10"},
        ),
        (
            "http://127.0.0.1:8765",
            ("127.0.0.1", 50000),
            {"X-Real-IP": "203.0.113.10"},
        ),
        (
            "http://127.0.0.1:8765",
            ("127.0.0.1", 50000),
            {"X-Forwarded-For": "203.0.113.10"},
        ),
        (
            "http://127.0.0.1:8765",
            ("127.0.0.1", 50000),
            {"X-Forwarded-For": ""},
        ),
        (
            "http://127.0.0.1:8765",
            ("127.0.0.1", 50000),
            {"X-Forwarded-Port": "443"},
        ),
    ),
)
def test_http_localhost_bypass_rejects_proxy_or_ambiguous_requests(
    tmp_path, monkeypatch, base_url, peer, headers
):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_MODE", "http")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "oauth")
    monkeypatch.setenv("WORKGATE_AUTH_BYPASS_LOCALHOST", "true")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    response = TestClient(
        build_http_app(), base_url=base_url, client=peer
    ).post("/tools/read", json={}, headers=headers)

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("peer", "host", "headers", "expected"),
    (
        (("localhost", 50000), "localhost", (), True),
        (("127.0.0.1", 50000), None, (), False),
        (("127.0.0.1", 50000), "[", (), False),
        (("127.0.0.1", 50000), "[::1]suffix", (), False),
        (("127.0.0.1", 50000), "::1", (), False),
        (("127.0.0.1", 50000), "127.0.0.1:bad", (), False),
        (("127.0.0.1", 50000), "not-an-ip", (), False),
        (None, "localhost", (), False),
        (("not-an-ip", 50000), "localhost", (), False),
        (
            ("127.0.0.1", 50000),
            "localhost",
            (("x-forwarded-prefix", ""),),
            False,
        ),
    ),
)
def test_direct_localhost_predicate_fails_closed_on_ambiguous_inputs(
    peer, host, headers, expected
):
    request = _auth_request(peer=peer, host=host, headers=headers)

    assert is_direct_localhost_request(request) is expected
