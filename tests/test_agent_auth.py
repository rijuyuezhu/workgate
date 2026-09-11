import asyncio
import json
import os
import stat
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

import workgate.agent_bridge.mcp as mcp_module
from workgate.agent_bridge.auth import (
    PersistentOAuthClientProvider,
    build_oauth_client_metadata,
    oauth_status,
    resolve_config_mapping,
)
from workgate.agent_bridge.auth_store import (
    AgentAuthStore,
    AgentAuthStoreCorruptError,
    AgentOAuthTokenStorage,
    AgentSecretNotFoundError,
)
from workgate.agent_bridge.cli import (
    LoopbackOAuthCallback,
)
from workgate.agent_bridge.mcp import AgentMcpClientManager
from workgate.agent_bridge.models import AgentMcpServerConfig
from workgate.agent_bridge.state import agent_registry_fingerprint
from workgate.agent_bridge.status import registry_config_status


def _client_info() -> OAuthClientInformationFull:
    return OAuthClientInformationFull.model_validate(
        {
            "client_id": "client-1",
            "client_secret": "client-secret",
            "redirect_uris": ["http://127.0.0.1/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        }
    )


def test_agent_auth_store_secret_lifecycle_permissions_and_no_value_listing(
    tmp_path,
):
    store = AgentAuthStore(tmp_path / "agent_auth")
    store.set_secret("docs", "api_token", "super-secret")

    assert store.get_secret("docs", "api_token") == "super-secret"
    assert store.list_secrets() == {"docs": ["api_token"]}
    assert "super-secret" not in json.dumps(store.list_secrets())
    assert store.delete_secret("docs", "api_token") is True
    assert store.delete_secret("docs", "api_token") is False
    with pytest.raises(AgentSecretNotFoundError, match="server docs"):
        store.get_secret("docs", "api_token")

    if os.name != "nt":
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o600


def test_agent_auth_store_corruption_is_explicit_and_not_reset(tmp_path):
    store = AgentAuthStore(tmp_path / "agent_auth")
    store.path.write_text("{broken", encoding="utf-8")

    with pytest.raises(AgentAuthStoreCorruptError, match="invalid"):
        store.list_secrets()
    assert store.path.read_text(encoding="utf-8") == "{broken"


def test_agent_auth_store_concurrent_mutations_preserve_all_secrets(tmp_path):
    root = tmp_path / "agent_auth"
    errors = []

    def writer(index: int) -> None:
        try:
            AgentAuthStore(root).set_secret(
                "docs", f"token_{index}", f"value-{index}"
            )
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(index,)) for index in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert AgentAuthStore(root).list_secrets() == {
        "docs": sorted(f"token_{index}" for index in range(12))
    }


def test_agent_oauth_storage_persists_expiry_client_and_refresh_token(tmp_path):
    store = AgentAuthStore(tmp_path / "agent_auth")
    store.set_client_info("docs", _client_info())
    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {
                "access_token": "access-one",
                "token_type": "Bearer",
                "expires_in": 60,
                "refresh_token": "refresh-one",
            }
        ),
    )
    first_expiry = store.oauth_expiry("docs")
    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {
                "access_token": "access-two",
                "token_type": "Bearer",
                "expires_in": 120,
            }
        ),
    )

    tokens = store.get_tokens("docs")
    assert tokens is not None
    assert tokens.access_token == "access-two"
    assert tokens.refresh_token == "refresh-one"
    assert store.oauth_redaction_values("docs") == {
        "oauth_access_token": "access-two",
        "oauth_refresh_token": "refresh-one",
        "oauth_client_secret": "client-secret",
    }
    client_info = store.get_client_info("docs")
    expiry = store.oauth_expiry("docs")
    assert client_info is not None
    assert client_info.client_id == "client-1"
    assert first_expiry is not None
    assert expiry is not None
    assert expiry > first_expiry
    metadata = store.oauth_metadata("docs")
    assert metadata["has_access_token"] is True
    assert metadata["has_refresh_token"] is True
    assert metadata["client_registered"] is True

    server = AgentMcpServerConfig.model_validate(
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "auth": {"mode": "oauth"},
        }
    )
    manager = AgentMcpClientManager(1, store)
    try:
        redaction_values, headers = manager.redaction_maps("docs", server)
    finally:
        manager.close()
    assert headers == {}
    assert set(redaction_values.values()) >= {
        "access-two",
        "refresh-one",
        "client-secret",
    }


def test_oauth_redaction_values_ignore_absent_optional_credentials(tmp_path):
    store = AgentAuthStore(tmp_path / "agent_auth")
    assert store.oauth_redaction_values("missing") == {}

    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {"access_token": "access-only", "token_type": "Bearer"}
        ),
    )
    store.set_client_info(
        "docs",
        OAuthClientInformationFull.model_validate(
            {
                "client_id": "public-client",
                "redirect_uris": ["http://127.0.0.1/callback"],
                "token_endpoint_auth_method": "none",
            }
        ),
    )

    assert store.oauth_redaction_values("docs") == {
        "oauth_access_token": "access-only"
    }


@pytest.mark.asyncio
async def test_persistent_oauth_provider_restores_absolute_expiry(tmp_path):
    store = AgentAuthStore(tmp_path / "agent_auth")
    store.set_client_info("docs", _client_info())
    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {
                "access_token": "access",
                "token_type": "Bearer",
                "expires_in": 60,
                "refresh_token": "refresh",
            }
        ),
    )
    storage = AgentOAuthTokenStorage(store, "docs")
    provider = PersistentOAuthClientProvider(
        "https://example.test/mcp",
        build_oauth_client_metadata("http://127.0.0.1/callback", []),
        storage,
    )

    await provider._initialize()

    assert provider.context.current_tokens is not None
    assert provider.context.current_tokens.access_token == "access"
    assert provider.context.token_expiry_time == store.oauth_expiry("docs")


def test_secret_references_resolve_only_from_private_store(tmp_path):
    store = AgentAuthStore(tmp_path / "agent_auth")
    store.set_secret("docs", "bearer", "Bearer private")
    server = AgentMcpServerConfig.model_validate(
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "headers": {
                "Authorization": {"secret": "bearer"},
                "X-Literal": "visible",
            },
            "auth": {"mode": "secret"},
        }
    )

    assert resolve_config_mapping(store, "docs", server.headers) == {
        "Authorization": "Bearer private",
        "X-Literal": "visible",
    }
    status = oauth_status(store, "docs", server)
    assert status["authorized"] is True
    assert "bearer" not in json.dumps(status)


def test_secret_reference_change_updates_registry_fingerprint(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = AgentAuthStore(tmp_path / "agent_auth")
    before = agent_registry_fingerprint(
        config_dir, (), store.fingerprint_paths()
    )
    store.set_secret("docs", "token", "one")
    after = agent_registry_fingerprint(
        config_dir, (), store.fingerprint_paths()
    )
    assert before != after


@pytest.mark.asyncio
async def test_stdio_transport_receives_resolved_secret_env(
    monkeypatch, tmp_path
):
    store = AgentAuthStore(tmp_path / "agent_auth")
    store.set_secret("stdio", "token", "private-value")
    server = AgentMcpServerConfig.model_validate(
        {
            "type": "stdio",
            "command": "fake-server",
            "env": {"TOKEN": {"secret": "token"}},
            "auth": {"mode": "secret"},
        }
    )
    captured = {}

    @asynccontextmanager
    async def fake_stdio(params):
        captured["env"] = params.env
        yield object(), object()

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def initialize(self):
            return None

        async def list_tools(self, *, params=None):
            return SimpleNamespace(tools=[], nextCursor=None)

    monkeypatch.setattr(mcp_module, "stdio_client", fake_stdio)
    monkeypatch.setattr(
        mcp_module, "ClientSession", lambda *_args: FakeSession()
    )

    manager = AgentMcpClientManager(1, store)
    try:
        assert await manager.list_tools("stdio", server) == []
        assert captured["env"] == {"TOKEN": "private-value"}
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_http_transport_receives_resolved_secret_header(
    monkeypatch, tmp_path
):
    store = AgentAuthStore(tmp_path / "agent_auth")
    store.set_secret("docs", "token", "Bearer private")
    server = AgentMcpServerConfig.model_validate(
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "headers": {"Authorization": {"secret": "token"}},
            "auth": {"mode": "secret"},
        }
    )
    captured = {}

    class FakeHttpClient:
        def __init__(self, *, headers=None, auth=None):
            captured["headers"] = headers
            captured["auth"] = auth

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    @asynccontextmanager
    async def fake_streamable(url, *, http_client):
        captured["url"] = url
        yield object(), object(), lambda: None

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def initialize(self):
            return None

        async def list_tools(self, *, params=None):
            return SimpleNamespace(tools=[], nextCursor=None)

    monkeypatch.setattr(mcp_module.httpx, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(mcp_module, "streamable_http_client", fake_streamable)
    monkeypatch.setattr(
        mcp_module, "ClientSession", lambda *_args: FakeSession()
    )

    manager = AgentMcpClientManager(1, store)
    assert await manager.list_tools("docs", server) == []
    assert captured["headers"] == {"Authorization": "Bearer private"}
    assert captured["auth"] is None


@pytest.mark.asyncio
async def test_loopback_oauth_callback_accepts_only_expected_path():
    async with LoopbackOAuthCallback() as callback:
        parsed = urlsplit(callback.redirect_uri)
        reader, writer = await asyncio.open_connection(
            parsed.hostname, parsed.port
        )
        writer.write(
            b"GET /callback?code=abc&state=state-1 HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()

        assert await callback.wait() == ("abc", "state-1")
        assert b"200 OK" in response


def test_public_registry_status_hides_secret_reference_names_and_values(
    tmp_path,
):
    from workgate.agent_bridge.registry import build_agent_registry

    config_dir = tmp_path / "agent_config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs": {
                        "type": "http",
                        "url": "https://example.test/mcp",
                        "enabled": False,
                        "headers": {
                            "Authorization": {"secret": "private_token_name"}
                        },
                        "auth": {"mode": "secret"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    store = AgentAuthStore(tmp_path / "agent_auth")
    store.set_secret("docs", "private_token_name", "Bearer private-value")
    registry = build_agent_registry(config_dir, AgentMcpClientManager(1, store))

    status = registry_config_status(registry)["mcp_servers"]["docs"]
    serialized = json.dumps(status)
    assert status["auth"] == {
        "mode": "secret",
        "authorized": True,
        "status": "configured",
        "secret_reference_count": 1,
        "missing_secret_count": 0,
        "expires_at": None,
    }
    assert status["headers"] == {"Authorization": "<redacted>"}
    assert "private_token_name" not in serialized
    assert "private-value" not in serialized


def test_resolve_secret_reference_requires_store():
    server = AgentMcpServerConfig.model_validate(
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "headers": {"Authorization": {"secret": "token"}},
            "auth": {"mode": "secret"},
        }
    )
    with pytest.raises(ValueError, match="no credential store"):
        resolve_config_mapping(None, "docs", server.headers)


def test_auth_status_covers_none_missing_refreshable_expired_and_authorized(
    tmp_path, monkeypatch
):
    none_server = AgentMcpServerConfig.model_validate(
        {"type": "http", "url": "https://example.test/mcp"}
    )
    secret_server = AgentMcpServerConfig.model_validate(
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "headers": {"Authorization": {"secret": "token"}},
            "auth": {"mode": "secret"},
        }
    )
    oauth_server = AgentMcpServerConfig.model_validate(
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "auth": {"mode": "oauth"},
        }
    )
    assert oauth_status(None, "docs", none_server)["status"] == "not_required"
    missing = oauth_status(None, "docs", secret_server)
    assert missing["status"] == "missing_secret"
    assert missing["missing_secret_count"] == 1

    store = AgentAuthStore(tmp_path / "agent_auth")
    monkeypatch.setattr("workgate.agent_bridge.auth.time.time", lambda: 100.0)

    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {
                "access_token": "expired",
                "refresh_token": "refresh",
                "token_type": "Bearer",
            }
        ),
    )
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["servers"]["docs"]["oauth"]["expires_at"] = 50.0
    store.path.write_text(json.dumps(data), encoding="utf-8")
    assert oauth_status(store, "docs", oauth_server)["status"] == "refreshable"

    store.clear_tokens("docs")
    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {"access_token": "expired", "token_type": "Bearer"}
        ),
    )
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["servers"]["docs"]["oauth"]["expires_at"] = 50.0
    store.path.write_text(json.dumps(data), encoding="utf-8")
    assert oauth_status(store, "docs", oauth_server)["status"] == "expired"

    data["servers"]["docs"]["oauth"]["expires_at"] = 150.0
    store.path.write_text(json.dumps(data), encoding="utf-8")
    assert oauth_status(store, "docs", oauth_server)["status"] == "authorized"


def test_auth_manager_compatibility_fallbacks_hide_failures():
    from workgate.agent_bridge.auth import (
        manager_auth_status,
        manager_redaction_maps,
    )

    server = AgentMcpServerConfig.model_validate(
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "headers": {"X-Literal": "visible"},
        }
    )

    class BrokenManager:
        def redaction_maps(self, _name, _server):
            raise RuntimeError("private-value")

        def auth_status(self, _name, _server):
            raise LookupError("private-value")

    assert manager_redaction_maps(BrokenManager(), "docs", server) == (
        {},
        {"X-Literal": "visible"},
    )
    status = manager_auth_status(BrokenManager(), "docs", server)
    assert status["status"] == "error"
    assert status["error"] == "LookupError"
    assert "private-value" not in json.dumps(status)


def test_build_oauth_provider_requires_url(tmp_path):
    from workgate.agent_bridge.auth import build_stored_oauth_provider

    server = AgentMcpServerConfig.model_construct(
        type="http",
        enabled=True,
        command=None,
        args=[],
        env={},
        url=None,
        headers={},
        auth={"mode": "oauth", "scopes": []},
    )
    with pytest.raises(ValueError, match="requires url"):
        build_stored_oauth_provider(
            AgentAuthStore(tmp_path / "agent_auth"), "docs", server
        )


def test_agent_auth_store_rejects_invalid_inputs_and_oversized_state(tmp_path):
    store = AgentAuthStore(tmp_path / "agent_auth")
    with pytest.raises(ValueError, match="server name"):
        store.set_secret("bad server", "token", "value")
    with pytest.raises(TypeError, match="text"):
        store.set_secret("docs", "token", 123)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must not be empty"):
        store.set_secret("docs", "token", "")
    with pytest.raises(ValueError, match="exceeds"):
        store.set_secret("docs", "token", "x" * 65_537)

    store.path.write_bytes(b"x" * 1_048_577)
    with pytest.raises(AgentAuthStoreCorruptError, match="exceeds"):
        store.list_secrets()


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"version": 2, "servers": {}}, "schema version"),
        ({"version": 1, "servers": []}, "servers must be an object"),
        ({"version": 1, "servers": {"bad server": {}}}, "server name"),
        ({"version": 1, "servers": {"docs": []}}, "must be an object"),
        ({"version": 1, "servers": {"docs": {"secrets": []}}}, "secrets"),
        (
            {"version": 1, "servers": {"docs": {"secrets": {"bad name": "x"}}}},
            "secret name",
        ),
        (
            {"version": 1, "servers": {"docs": {"secrets": {"token": 1}}}},
            "must be a string",
        ),
        (
            {
                "version": 1,
                "servers": {"docs": {"secrets": {"token": "x" * 65_537}}},
            },
            "too large",
        ),
        ({"version": 1, "servers": {"docs": {"oauth": []}}}, "OAuth state"),
        (
            {"version": 1, "servers": {"docs": {"oauth": {"tokens": []}}}},
            "OAuth tokens",
        ),
        (
            {"version": 1, "servers": {"docs": {"oauth": {"stored_at": "x"}}}},
            "stored_at",
        ),
    ],
)
def test_agent_auth_store_rejects_invalid_schema(tmp_path, payload, match):
    store = AgentAuthStore(tmp_path / "agent_auth")
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AgentAuthStoreCorruptError, match=match):
        store.list_secrets()


def test_agent_auth_store_rejects_invalid_oauth_models(tmp_path):
    store = AgentAuthStore(tmp_path / "agent_auth")
    for key, getter, match in [
        ("tokens", store.get_tokens, "tokens"),
        ("client_info", store.get_client_info, "client information"),
        (
            "authorization_metadata",
            store.get_authorization_metadata,
            "authorization metadata",
        ),
    ]:
        store.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "servers": {"docs": {"oauth": {key: {"invalid": True}}}},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(AgentAuthStoreCorruptError, match=match):
            getter("docs")


def test_agent_auth_store_clear_tokens_and_oauth_prune_entries(tmp_path):
    store = AgentAuthStore(tmp_path / "agent_auth")
    assert store.clear_tokens("docs") is False
    assert store.clear_oauth("docs") is False
    store.set_client_info("docs", _client_info())
    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {"access_token": "access", "token_type": "Bearer"}
        ),
    )
    assert store.clear_tokens("docs") is True
    assert store.get_tokens("docs") is None
    assert store.get_client_info("docs") is not None
    assert store.clear_oauth("docs") is True
    assert json.loads(store.path.read_text(encoding="utf-8"))["servers"] == {}
