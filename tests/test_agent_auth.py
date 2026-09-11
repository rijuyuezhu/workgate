import asyncio
import json
import os
import stat
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

import workgate.agent_bridge.auth_store as auth_store_module
import workgate.agent_bridge.mcp as mcp_module
from workgate.agent_bridge.auth import (
    PersistentOAuthClientProvider,
    build_oauth_client_metadata,
    oauth_status,
    resolve_config_mapping,
)
from workgate.agent_bridge.auth_store import (
    AgentAuthRedactionHistoryLostError,
    AgentAuthStore,
    AgentAuthStoreCorruptError,
    AgentAuthStoreError,
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


def test_credential_redaction_history_captures_intermediate_mutations_and_is_bounded(
    tmp_path, monkeypatch
):
    store = AgentAuthStore(tmp_path / "agent_auth")
    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {"access_token": "old-token", "token_type": "Bearer"}
        ),
    )
    cursor = store.redaction_cursor("docs")
    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {"access_token": "mid-token", "token_type": "Bearer"}
        ),
    )
    store.set_client_info("docs", _client_info())
    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {"access_token": "new-token", "token_type": "Bearer"}
        ),
    )

    assert set(
        store.credential_redaction_values_since("docs", cursor).values()
    ) == {
        "old-token",
        "mid-token",
        "client-secret",
        "new-token",
    }

    store.set_secret("secret", "token", "old-secret")
    secret_cursor = store.redaction_cursor("secret")
    store.set_secret("secret", "token", "new-secret")
    assert set(
        store.credential_redaction_values_since(
            "secret", secret_cursor
        ).values()
    ) == {"old-secret", "new-secret"}
    with pytest.raises(ValueError, match="non-negative"):
        store.credential_redaction_values_since("docs", -1)
    with pytest.raises(ValueError, match="newer than credential history"):
        store.credential_redaction_values_since(
            "docs", store.redaction_cursor("docs") + 1
        )

    monkeypatch.setattr(auth_store_module, "_MAX_REDACTION_HISTORY_ENTRIES", 1)
    bounded = AgentAuthStore(tmp_path / "bounded_auth")
    bounded.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {"access_token": "old-bounded", "token_type": "Bearer"}
        ),
    )
    stale_cursor = bounded.redaction_cursor("docs")
    for token in ("mid-bounded", "new-bounded"):
        bounded.set_tokens(
            "docs",
            OAuthToken.model_validate(
                {"access_token": token, "token_type": "Bearer"}
            ),
        )
    with pytest.raises(
        AgentAuthRedactionHistoryLostError, match="history unavailable"
    ):
        bounded.credential_redaction_values_since("docs", stale_cursor)


def test_credential_redaction_history_size_eviction_keeps_mutation_available(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(auth_store_module, "_MAX_STORE_BYTES", 1_400)
    store = AgentAuthStore(tmp_path / "agent_auth")
    old_secret = "A" * 400
    new_secret = "B" * 400
    store.set_secret("docs", "token", old_secret)
    store.set_secret("docs", "token", new_secret)

    assert store.get_secret("docs", "token") == new_secret
    assert store.path.stat().st_size <= 1_400
    with pytest.raises(
        AgentAuthRedactionHistoryLostError, match="history unavailable"
    ):
        store.credential_redaction_values_since("docs", 0)


def test_credential_redaction_history_eviction_preserves_store_size_limit(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(auth_store_module, "_MAX_STORE_BYTES", 350)
    store = AgentAuthStore(tmp_path / "agent_auth")

    with pytest.raises(AgentAuthStoreError, match="would exceed"):
        store.set_secret("docs", "token", "A" * 300)


def test_credential_redaction_history_gap_allows_new_writes_but_fails_closed(
    tmp_path,
):
    store = AgentAuthStore(tmp_path / "agent_auth")
    store.path.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": {"docs": {"secrets": {"token": "midMixedB2"}}},
                "credential_revisions": {"docs": 2},
                "credential_redaction_history": {
                    "docs": [{"revision": 1, "values": ["oldMixedA1"]}]
                },
            }
        ),
        encoding="utf-8",
    )

    store.set_secret("docs", "token", "newMixedC3")
    assert store.get_secret("docs", "token") == "newMixedC3"
    assert store.redaction_cursor("docs") == 3
    with pytest.raises(
        AgentAuthRedactionHistoryLostError, match="history unavailable"
    ):
        store.credential_redaction_values_since("docs", 0)


def test_credential_redaction_history_survives_cross_instance_writes(tmp_path):
    root = tmp_path / "agent_auth"
    service_store = AgentAuthStore(root)
    cli_store = AgentAuthStore(root)

    service_store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {"access_token": "oldOpaqueA1", "token_type": "Bearer"}
        ),
    )
    cursor = service_store.redaction_cursor("docs")
    cli_store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {"access_token": "midOpaqueB2", "token_type": "Bearer"}
        ),
    )
    observed_tokens = service_store.get_tokens("docs")
    assert observed_tokens is not None
    assert observed_tokens.access_token == "midOpaqueB2"
    cli_store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {"access_token": "newOpaqueC3", "token_type": "Bearer"}
        ),
    )

    assert set(
        service_store.credential_redaction_values_since("docs", cursor).values()
    ) == {"oldOpaqueA1", "midOpaqueB2", "newOpaqueC3"}

    service_store.set_secret("secret", "token", "oldSecretA1")
    secret_cursor = service_store.redaction_cursor("secret")
    cli_store.set_secret("secret", "token", "midSecretB2")
    assert service_store.get_secret("secret", "token") == "midSecretB2"
    cli_store.set_secret("secret", "token", "newSecretC3")
    assert set(
        service_store.credential_redaction_values_since(
            "secret", secret_cursor
        ).values()
    ) == {"oldSecretA1", "midSecretB2", "newSecretC3"}


def test_manifest_literal_redaction_history_is_durable_and_deduplicated(
    tmp_path,
):
    root = tmp_path / "agent_auth"
    first_store = AgentAuthStore(root)
    second_store = AgentAuthStore(root)

    assert (
        first_store.observe_redaction_values("docs", ("oldLiteralA1",)) is True
    )
    assert first_store.redaction_cursor("docs") == 1
    assert (
        first_store.observe_redaction_values("docs", ("oldLiteralA1",)) is False
    )
    assert first_store.redaction_cursor("docs") == 1

    assert (
        second_store.observe_redaction_values(
            "docs", ("oldLiteralA1", "newLiteralB2")
        )
        is True
    )
    assert second_store.redaction_cursor("docs") == 2
    assert set(
        AgentAuthStore(root)
        .credential_redaction_values_since("docs", 0)
        .values()
    ) == {"oldLiteralA1", "newLiteralB2"}


@pytest.mark.parametrize(
    ("transport", "field", "old_value", "new_value"),
    [
        ("http", "headers", "oldHeaderA1", "newHeaderB2"),
        ("stdio", "env", "oldEnvA1", "newEnvB2"),
    ],
)
def test_mcp_manager_reconstructs_manifest_literal_history_after_restart(
    tmp_path, transport, field, old_value, new_value
):
    root = tmp_path / "agent_auth"

    def server(value: str) -> AgentMcpServerConfig:
        payload = {
            "type": transport,
            field: {"Authorization" if field == "headers" else "TOKEN": value},
        }
        if transport == "http":
            payload["url"] = "https://example.test/mcp"
        else:
            payload["command"] = "example-mcp"
        return AgentMcpServerConfig.model_validate(payload)

    first_manager = AgentMcpClientManager(1, AgentAuthStore(root))
    try:
        assert first_manager.redaction_cursor("docs", server(old_value)) == 0
    finally:
        first_manager.close()

    restarted_manager = AgentMcpClientManager(1, AgentAuthStore(root))
    try:
        current = server(new_value)
        baseline = restarted_manager.redaction_cursor("docs", current)
        assert baseline == 0
        env, headers = restarted_manager.redaction_maps_since(
            "docs", current, baseline
        )
        assert {old_value, new_value} <= set((*env.values(), *headers.values()))
    finally:
        restarted_manager.close()


def test_durable_redaction_values_do_not_replace_current_env_redaction(
    tmp_path,
):
    root = tmp_path / "agent_auth"
    store = AgentAuthStore(root)
    store.path.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": {
                    "docs": {
                        "secrets": {"token": "activeLegacySecretA1"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert store.observe_redaction_values("docs", ("retiredLiteralB2",)) is True
    server = AgentMcpServerConfig.model_validate(
        {
            "type": "stdio",
            "command": "example-mcp",
            "env": {"credential_observed_0": {"secret": "token"}},
            "auth": {"mode": "secret"},
        }
    )
    manager = AgentMcpClientManager(1, store)
    try:
        baseline = manager.redaction_cursor("docs", server)
        env, _headers = manager.redaction_maps_since("docs", server, baseline)
        assert {"activeLegacySecretA1", "retiredLiteralB2"} <= set(env.values())
    finally:
        manager.close()


def test_credential_redaction_history_survives_child_process_writer(tmp_path):
    root = tmp_path / "agent_auth"
    old_token = "oldChildA1"
    mid_token = "midChildB2"
    new_token = "newChildC3"
    AgentAuthStore(root).set_tokens(
        "docs",
        OAuthToken.model_validate(
            {"access_token": old_token, "token_type": "Bearer"}
        ),
    )

    child_code = """
import sys
from pathlib import Path
from mcp.shared.auth import OAuthToken
from workgate.agent_bridge.auth_store import AgentAuthStore

store = AgentAuthStore(Path(sys.argv[1]))
for value in sys.argv[2:]:
    store.set_tokens(
        "docs",
        OAuthToken.model_validate(
            {"access_token": value, "token_type": "Bearer"}
        ),
    )
"""
    subprocess.run(
        [sys.executable, "-c", child_code, str(root), mid_token, new_token],
        check=True,
    )

    restarted_store = AgentAuthStore(root)
    restarted_tokens = restarted_store.get_tokens("docs")
    assert restarted_tokens is not None
    assert restarted_tokens.access_token == new_token
    assert set(
        restarted_store.credential_redaction_values_since("docs", 0).values()
    ) == {old_token, mid_token, new_token}


def test_mcp_manager_retains_credential_redaction_history_across_operations(
    tmp_path,
):
    root = tmp_path / "agent_auth"
    store = AgentAuthStore(root)
    external_store = AgentAuthStore(root)
    server = AgentMcpServerConfig.model_validate(
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "auth": {"mode": "oauth"},
        }
    )

    def token(value: str) -> OAuthToken:
        return OAuthToken.model_validate(
            {"access_token": value, "token_type": "Bearer"}
        )

    old_token = "oldRetiredA1"
    mid_token = "midRetiredB2"
    new_token = "newRetiredC3"
    store.set_tokens("docs", token(old_token))
    manager = AgentMcpClientManager(1, store)
    try:
        baseline = manager.redaction_cursor("docs", server)
        assert baseline == 0

        store.set_tokens("docs", token(mid_token))
        first_values = set(
            manager.redaction_maps_since("docs", server, baseline)[0].values()
        )
        assert {old_token, mid_token} <= first_values

        assert manager.redaction_cursor("docs", server) == baseline
        store.set_tokens("docs", token(new_token))
        second_values = set(
            manager.redaction_maps_since("docs", server, baseline)[0].values()
        )
        assert {old_token, mid_token, new_token} <= second_values

        third_cursor = manager.redaction_cursor("docs", server)
        assert third_cursor == baseline
        third_values = set(
            manager.redaction_maps_since("docs", server, third_cursor)[
                0
            ].values()
        )
        assert {old_token, mid_token, new_token} <= third_values

        external_token = "externalRetiredD4"
        external_store.set_tokens("docs", token(external_token))
        assert manager.redaction_cursor("docs", server) == baseline
        external_values = set(
            manager.redaction_maps_since("docs", server, baseline)[0].values()
        )
        assert {
            old_token,
            mid_token,
            new_token,
            external_token,
        } <= external_values
    finally:
        manager.close()


def test_mcp_manager_anchors_redaction_from_zero_before_initial_authorization(
    tmp_path,
):
    root = tmp_path / "agent_auth"
    service_store = AgentAuthStore(root)
    cli_store = AgentAuthStore(root)
    server = AgentMcpServerConfig.model_validate(
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "auth": {"mode": "oauth"},
        }
    )
    manager = AgentMcpClientManager(1, service_store)
    try:
        assert manager.redaction_cursor("docs", server) == 0
        cli_store.set_tokens(
            "docs",
            OAuthToken.model_validate(
                {"access_token": "firstAuthorizedA1", "token_type": "Bearer"}
            ),
        )

        baseline = manager.redaction_cursor("docs", server)
        assert baseline == 0
        values = set(
            manager.redaction_maps_since("docs", server, baseline)[0].values()
        )
        assert "firstAuthorizedA1" in values
    finally:
        manager.close()


def test_mcp_manager_reconstructs_durable_redaction_history_after_restart(
    tmp_path,
):
    root = tmp_path / "agent_auth"
    server = AgentMcpServerConfig.model_validate(
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "auth": {"mode": "oauth"},
        }
    )

    def token(value: str) -> OAuthToken:
        return OAuthToken.model_validate(
            {"access_token": value, "token_type": "Bearer"}
        )

    old_token = "oldRestartA1"
    mid_token = "midRestartB2"
    new_token = "newRestartC3"
    store = AgentAuthStore(root)
    store.set_tokens("docs", token(old_token))
    first_manager = AgentMcpClientManager(1, store)
    try:
        assert first_manager.redaction_cursor("docs", server) == 0
        store.set_tokens("docs", token(mid_token))
        store.set_tokens("docs", token(new_token))
    finally:
        first_manager.close()

    restarted_store = AgentAuthStore(root)
    restarted_manager = AgentMcpClientManager(1, restarted_store)
    try:
        baseline = restarted_manager.redaction_cursor("docs", server)
        assert baseline == 0
        values = set(
            restarted_manager.redaction_maps_since("docs", server, baseline)[
                0
            ].values()
        )
        assert {old_token, mid_token, new_token} <= values
    finally:
        restarted_manager.close()


def test_mcp_manager_fails_closed_when_durable_revision_history_is_missing(
    tmp_path,
):
    root = tmp_path / "agent_auth"
    store = AgentAuthStore(root)
    store.path.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": {"docs": {"secrets": {"token": "currentSecretC3"}}},
                "credential_revisions": {"docs": 2},
            }
        ),
        encoding="utf-8",
    )
    server = AgentMcpServerConfig.model_validate(
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "headers": {"Authorization": {"secret": "token"}},
            "auth": {"mode": "secret"},
        }
    )
    manager = AgentMcpClientManager(1, AgentAuthStore(root))
    try:
        with pytest.raises(
            AgentAuthRedactionHistoryLostError, match="history unavailable"
        ):
            manager.redaction_cursor("docs", server)
    finally:
        manager.close()


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
        (
            {"version": 1, "servers": {}, "credential_revisions": []},
            "credential revisions",
        ),
        (
            {
                "version": 1,
                "servers": {},
                "credential_revisions": {"docs": -1},
            },
            "non-negative integer",
        ),
        (
            {
                "version": 1,
                "servers": {},
                "credential_revisions": {"bad server": 1},
            },
            "server name",
        ),
        (
            {
                "version": 1,
                "servers": {},
                "credential_redaction_history": [],
            },
            "redaction history must be an object",
        ),
        (
            {
                "version": 1,
                "servers": {},
                "credential_revisions": {"docs": 1},
                "credential_redaction_history": {"docs": {}},
            },
            "must be a list",
        ),
        (
            {
                "version": 1,
                "servers": {},
                "credential_redaction_history": {"bad server": []},
            },
            "server name",
        ),
        (
            {
                "version": 1,
                "servers": {},
                "credential_revisions": {"docs": 257},
                "credential_redaction_history": {
                    "docs": [
                        {"revision": revision, "values": []}
                        for revision in range(1, 258)
                    ]
                },
            },
            "retention limit",
        ),
        (
            {
                "version": 1,
                "servers": {},
                "credential_revisions": {"docs": 1},
                "credential_redaction_history": {"docs": [None]},
            },
            "is invalid",
        ),
        (
            {
                "version": 1,
                "servers": {},
                "credential_revisions": {"docs": 1},
                "credential_redaction_history": {
                    "docs": [{"revision": 0, "values": []}]
                },
            },
            "positive integer",
        ),
        (
            {
                "version": 1,
                "servers": {},
                "credential_revisions": {"docs": 2},
                "credential_redaction_history": {
                    "docs": [
                        {"revision": 2, "values": []},
                        {"revision": 1, "values": []},
                    ]
                },
            },
            "strictly increasing",
        ),
        (
            {
                "version": 1,
                "servers": {},
                "credential_revisions": {"docs": 1},
                "credential_redaction_history": {
                    "docs": [{"revision": 2, "values": []}]
                },
            },
            "exceeds the current revision",
        ),
        (
            {
                "version": 1,
                "servers": {},
                "credential_revisions": {"docs": 1},
                "credential_redaction_history": {
                    "docs": [{"revision": 1, "values": "secret"}]
                },
            },
            "redaction values",
        ),
        (
            {
                "version": 1,
                "servers": {},
                "credential_revisions": {"docs": 1},
                "credential_redaction_history": {
                    "docs": [{"revision": 1, "values": [""]}]
                },
            },
            "non-empty text",
        ),
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
