"""Agent Bridge secret resolution and MCP SDK OAuth integration."""

import time
from collections.abc import Callable, Mapping
from typing import Any, cast

import httpx
from mcp.client.auth.oauth2 import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata

from .auth_store import AgentAuthStore, AgentOAuthTokenStorage
from .models import AgentConfigValue, AgentMcpServerConfig, AgentSecretReference

type OAuthProviderFactory = Callable[[str, AgentMcpServerConfig], httpx.Auth]


def literal_config_mapping(
    values: Mapping[str, AgentConfigValue],
) -> dict[str, str]:
    """Return literal manifest values, excluding structured secret references."""
    return {
        key: value for key, value in values.items() if isinstance(value, str)
    }


def secret_reference_names(
    values: Mapping[str, AgentConfigValue],
) -> tuple[str, ...]:
    """Return private-store reference names without resolving their values."""
    return tuple(
        value.secret
        for value in values.values()
        if isinstance(value, AgentSecretReference)
    )


def resolve_config_mapping(
    store: AgentAuthStore | None,
    server_name: str,
    values: Mapping[str, AgentConfigValue],
) -> dict[str, str]:
    """Resolve literal and private secret values immediately before transport use."""
    resolved: dict[str, str] = {}
    for key, value in values.items():
        if isinstance(value, str):
            resolved[key] = value
            continue
        if store is None:
            raise ValueError(
                f"Agent Bridge server {server_name} uses secret references but no credential store is configured"
            )
        resolved[key] = store.get_secret(server_name, value.secret)
    return resolved


class PersistentOAuthClientProvider(OAuthClientProvider):
    """SDK OAuth provider that restores absolute token expiry from private storage."""

    def __init__(
        self,
        server_url: str,
        client_metadata: OAuthClientMetadata,
        storage: AgentOAuthTokenStorage,
        **kwargs: Any,
    ) -> None:
        super().__init__(server_url, client_metadata, storage, **kwargs)
        self._persistent_storage = storage

    async def _initialize(self) -> None:
        await super()._initialize()
        self.context.token_expiry_time = self._persistent_storage.expiry()
        metadata = self._persistent_storage.authorization_metadata()
        if metadata is not None:
            self.context.oauth_metadata = metadata
            self.context.auth_server_url = str(metadata.issuer)

    def _persist_authorization_metadata(self) -> None:
        if self.context.oauth_metadata is not None:
            self._persistent_storage.set_authorization_metadata(
                self.context.oauth_metadata
            )

    async def _handle_token_response(self, response: httpx.Response) -> None:
        await super()._handle_token_response(response)
        self._persist_authorization_metadata()

    async def _handle_refresh_response(self, response: httpx.Response) -> bool:
        result = await super()._handle_refresh_response(response)
        if result:
            self._persist_authorization_metadata()
        else:
            self._persistent_storage.store.clear_tokens(
                self._persistent_storage.server
            )
        return result


def build_oauth_client_metadata(
    redirect_uri: str, scopes: list[str]
) -> OAuthClientMetadata:
    """Build the public OAuth client metadata shared by runtime and CLI flows."""
    return OAuthClientMetadata.model_validate(
        {
            "client_name": "workgate Agent Bridge",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": " ".join(scopes) if scopes else None,
        }
    )


def build_stored_oauth_provider(
    store: AgentAuthStore,
    server_name: str,
    server: AgentMcpServerConfig,
    *,
    redirect_uri: str = "http://127.0.0.1/callback",
    redirect_handler=None,
    callback_handler=None,
    timeout: float = 300,
) -> PersistentOAuthClientProvider:
    """Create the MCP SDK provider for one stored Agent Bridge OAuth identity."""
    if not server.url:
        raise ValueError(f"OAuth MCP server {server_name} requires url")
    storage = AgentOAuthTokenStorage(store, server_name)

    async def deny_redirect(_url: str) -> None:
        raise RuntimeError(
            f"OAuth reauthorization required for {server_name}; run workgate mcp auth {server_name}"
        )

    async def deny_callback() -> tuple[str, str | None]:
        raise RuntimeError(
            f"OAuth reauthorization required for {server_name}; run workgate mcp auth {server_name}"
        )

    effective_redirect_handler = redirect_handler or deny_redirect
    effective_callback_handler = callback_handler or deny_callback
    return PersistentOAuthClientProvider(
        server.url,
        build_oauth_client_metadata(redirect_uri, server.auth.scopes),
        storage,
        redirect_handler=effective_redirect_handler,
        callback_handler=effective_callback_handler,
        timeout=timeout,
    )


def oauth_status(
    store: AgentAuthStore | None,
    server_name: str,
    server: AgentMcpServerConfig,
) -> dict[str, Any]:
    """Return public auth metadata without secret, token, or client values."""
    mode = server.auth.mode
    if mode == "none":
        return {
            "mode": mode,
            "authorized": True,
            "status": "not_required",
            "expires_at": None,
        }
    if mode == "secret":
        references = secret_reference_names(
            server.env
        ) + secret_reference_names(server.headers)
        if store is None:
            missing = len(references)
        else:
            missing = sum(
                not store.has_secret(server_name, reference)
                for reference in references
            )
        return {
            "mode": mode,
            "authorized": bool(references) and missing == 0,
            "status": "configured"
            if references and missing == 0
            else "missing_secret",
            "secret_reference_count": len(references),
            "missing_secret_count": missing,
            "expires_at": None,
        }
    if store is None:
        metadata: dict[str, Any] = {}
    else:
        metadata = store.oauth_metadata(server_name)
    expires_at = metadata.get("expires_at")
    has_access = bool(metadata.get("has_access_token"))
    has_refresh = bool(metadata.get("has_refresh_token"))
    expired = bool(expires_at is not None and expires_at <= time.time())
    authorized = has_access and (not expired or has_refresh)
    if not has_access:
        status = "unauthorized"
    elif expired and has_refresh:
        status = "refreshable"
    elif expired:
        status = "expired"
    else:
        status = "authorized"
    return {
        "mode": mode,
        "authorized": authorized,
        "status": status,
        "expires_at": expires_at,
        "client_registered": bool(metadata.get("client_registered")),
    }


def manager_redaction_maps(
    manager: Any,
    server_name: str,
    server: AgentMcpServerConfig,
) -> tuple[dict[str, str], dict[str, str]]:
    """Read transport values for redaction, with compatibility for test managers."""
    method = getattr(manager, "redaction_maps", None)
    if callable(method):
        try:
            return cast(
                tuple[dict[str, str], dict[str, str]],
                method(server_name, server),
            )
        except Exception:
            pass
    return literal_config_mapping(server.env), literal_config_mapping(
        server.headers
    )


def manager_redaction_cursor(
    manager: Any,
    server_name: str,
    server: AgentMcpServerConfig,
) -> Any:
    """Start operation-local credential observation when the manager supports it."""
    method = getattr(manager, "redaction_cursor", None)
    if callable(method):
        return method(server_name, server)
    return None


def manager_redaction_maps_since(
    manager: Any,
    server_name: str,
    server: AgentMcpServerConfig,
    cursor: Any,
) -> tuple[dict[str, str], dict[str, str]]:
    """Read all values observed during one operation, or fall back to current maps."""
    method = getattr(manager, "redaction_maps_since", None)
    if callable(method):
        return cast(
            tuple[dict[str, str], dict[str, str]],
            method(server_name, server, cursor),
        )
    return manager_redaction_maps(manager, server_name, server)


def manager_auth_status(
    manager: Any,
    server_name: str,
    server: AgentMcpServerConfig,
) -> dict[str, Any]:
    """Read public auth status, including compatibility for injected test managers."""
    method = getattr(manager, "auth_status", None)
    if callable(method):
        try:
            return cast(dict[str, Any], method(server_name, server))
        except Exception as exc:
            return {
                "mode": server.auth.mode,
                "authorized": False,
                "status": "error",
                "expires_at": None,
                "error": type(exc).__name__,
            }
    return oauth_status(None, server_name, server)
