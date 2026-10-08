"""Normalize upstream MCP protocol objects and manage client sessions for configured agent bridge servers."""

import asyncio
import json
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import PaginatedRequestParams

from ..utils.serialization import to_jsonable
from .auth import (
    OAuthProviderFactory,
    build_stored_oauth_provider,
    credential_store_key,
    oauth_status,
    resolve_config_mapping,
    sensitive_literal_config_mapping,
)
from .auth_store import AgentAuthStore
from .models import AgentMcpServerConfig


def _extend_redaction_map(
    mapping: Mapping[str, str], values: Mapping[str, str]
) -> dict[str, str]:
    """Append redaction-only values without clobbering real transport keys."""
    extended = dict(mapping)
    index = 0
    for value in values.values():
        while (key := f"credential_observed_{index}") in extended:
            index += 1
        extended[key] = value
        index += 1
    return extended


@dataclass(frozen=True)
class AgentMcpTool:
    """Normalized description of an upstream MCP tool exposed through the bridge."""

    name: str
    """Upstream MCP tool name."""
    description: str
    """Human-readable tool description advertised by the upstream server."""
    input_schema: dict[str, Any]
    """JSON schema describing the upstream tool input payload."""


def _value(source: Any, name: str, default: Any = None) -> Any:
    """Read an MCP protocol field from either a mapping or SDK object."""
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def normalize_mcp_tool(tool: Any) -> AgentMcpTool:
    """Normalize SDK-specific MCP tool objects into a stable serializable shape."""
    input_schema = _value(tool, "inputSchema")
    if input_schema is None:
        input_schema = _value(tool, "input_schema", {})

    return AgentMcpTool(
        name=str(_value(tool, "name", "")),
        description=str(_value(tool, "description", "") or ""),
        input_schema=input_schema,
    )


def _normalize_content_item(item: Any) -> Any:
    """Convert MCP content blocks to JSON-serializable dictionaries while preserving unknown fields."""
    if _value(item, "type") == "text":
        return {"type": "text", "text": _value(item, "text", "")}
    jsonable_item = to_jsonable(item)
    if isinstance(jsonable_item, dict):
        return jsonable_item
    return {"type": "repr", "repr": repr(item)}


def normalize_tool_result(result: Any) -> dict[str, Any]:
    """Convert an MCP tool result into a stable payload with content blocks and error state."""
    structured_content = _value(result, "structuredContent")
    if structured_content is None:
        structured_content = _value(result, "structured_content")
    structured_content = to_jsonable(structured_content)

    return {
        "is_error": bool(
            _value(result, "isError", False)
            or _value(result, "is_error", False)
        ),
        "content": [
            _normalize_content_item(item)
            for item in _value(result, "content", [])
        ],
        "structured_content": structured_content,
    }


# Bound both the upstream transfer and any retained discovery metadata.
MAX_TOOLS_PER_SERVER = 100
MAX_TOOL_DESCRIPTOR_BYTES = 16 * 1024
MAX_SERVER_DESCRIPTOR_BYTES = 256 * 1024


def _tool_descriptor_bytes(tool: AgentMcpTool) -> int:
    return len(
        json.dumps(
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            },
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    )


async def _list_session_tools(session: ClientSession) -> list[AgentMcpTool]:
    """Page through one MCP server's bounded tool catalog."""
    tools: list[AgentMcpTool] = []
    catalog_bytes = 0
    cursor: str | None = None
    while True:
        result = await session.list_tools(
            params=PaginatedRequestParams(cursor=cursor)
        )
        for item in _value(result, "tools", result):
            if len(tools) >= MAX_TOOLS_PER_SERVER:
                raise ValueError("MCP server exceeds tool count limit")
            tool = normalize_mcp_tool(item)
            size = _tool_descriptor_bytes(tool)
            if size > MAX_TOOL_DESCRIPTOR_BYTES:
                raise ValueError("MCP tool descriptor exceeds size limit")
            catalog_bytes += size
            if catalog_bytes > MAX_SERVER_DESCRIPTOR_BYTES:
                raise ValueError("MCP server tool catalog exceeds size limit")
            tools.append(tool)
        cursor = getattr(result, "nextCursor", None)
        if not cursor:
            return tools


class AgentMcpClientManager:
    """Open bounded, short-lived MCP connections for each discovery or call."""

    def __init__(
        self,
        call_timeout_s: float = 60,
        auth_store: AgentAuthStore | None = None,
        oauth_provider_factory: OAuthProviderFactory | None = None,
        *,
        allow_stdio: bool = True,
        stdio_cwd: str | None = None,
    ) -> None:
        self.call_timeout_s = call_timeout_s
        self.auth_store = auth_store
        self.oauth_provider_factory = oauth_provider_factory
        self.allow_stdio = allow_stdio
        self.stdio_cwd = stdio_cwd

    def resolved_maps(
        self, name: str, server: AgentMcpServerConfig
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Resolve transport env/headers immediately before opening a connection."""
        credential_key = credential_store_key(name, server)
        return (
            resolve_config_mapping(self.auth_store, credential_key, server.env),
            resolve_config_mapping(
                self.auth_store, credential_key, server.headers
            ),
        )

    def redaction_maps(
        self, name: str, server: AgentMcpServerConfig
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Resolve transport and owner-held credential values solely for redaction."""
        env, headers = self.resolved_maps(name, server)
        if self.auth_store is not None and server.auth.mode == "oauth":
            credential_key = credential_store_key(name, server)
            env = {
                **env,
                **self.auth_store.oauth_redaction_values(credential_key),
            }
        return env, headers

    def redaction_cursor(
        self, name: str, server: AgentMcpServerConfig
    ) -> int | None:
        """Validate this integration's durable private-value history before use."""
        if self.auth_store is None:
            return None
        if (
            server.integration_id is None
            and not server.requires_stable_integration_id()
        ):
            return None
        credential_key = credential_store_key(name, server)
        literal_values = tuple(
            sensitive_literal_config_mapping(server.env).values()
        ) + tuple(sensitive_literal_config_mapping(server.headers).values())
        self.auth_store.observe_redaction_values(credential_key, literal_values)
        # The stable integration identity, rather than the mutable manifest label,
        # is the confidentiality and fail-closed boundary.
        self.auth_store.credential_redaction_values_since(credential_key, 0)
        return 0

    def redaction_maps_since(
        self,
        name: str,
        server: AgentMcpServerConfig,
        cursor: int | None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Resolve current and durable retained credentials since the safe baseline."""
        env, headers = self.redaction_maps(name, server)
        if cursor is None or self.auth_store is None:
            return env, headers
        credential_key = credential_store_key(name, server)
        observed = self.auth_store.credential_redaction_values_since(
            credential_key, cursor
        )
        return _extend_redaction_map(env, observed), headers

    def auth_status(
        self, name: str, server: AgentMcpServerConfig
    ) -> dict[str, Any]:
        """Return public authorization status for registry payloads."""
        return oauth_status(self.auth_store, name, server)

    def _oauth_auth(
        self, name: str, server: AgentMcpServerConfig
    ) -> httpx.Auth | None:
        if server.auth.mode != "oauth":
            return None
        if self.oauth_provider_factory is not None:
            return self.oauth_provider_factory(name, server)
        if self.auth_store is None:
            raise ValueError(
                f"OAuth authorization required for {name}; run workgate mcp auth {name}"
            )
        status = self.auth_status(name, server)
        if not status["authorized"]:
            raise ValueError(
                f"OAuth authorization required for {name}; run workgate mcp auth {name}"
            )
        return build_stored_oauth_provider(self.auth_store, name, server)

    @asynccontextmanager
    async def _session(
        self, name: str, server: AgentMcpServerConfig
    ) -> AsyncGenerator[ClientSession]:
        """Open and initialize the transport-specific MCP client session for one configured server."""
        env, headers = self.resolved_maps(name, server)
        auth = self._oauth_auth(name, server)
        match server.type:
            case "stdio":
                if not self.allow_stdio:
                    raise ValueError("stdio MCP servers are executor-owned")
                if not server.command:
                    raise ValueError("stdio MCP server requires command")
                params = StdioServerParameters(
                    command=server.command,
                    args=list(server.args),
                    env=env or None,
                    cwd=self.stdio_cwd,
                )
                async with (
                    stdio_client(params) as (read_stream, write_stream),
                    ClientSession(read_stream, write_stream) as session,
                ):
                    await session.initialize()
                    yield session
            case "http":
                if not server.url:
                    raise ValueError("http MCP server requires url")
                async with (
                    httpx.AsyncClient(
                        headers=headers or None, auth=auth
                    ) as client,
                    streamable_http_client(server.url, http_client=client) as (
                        read_stream,
                        write_stream,
                        _get_session_id,
                    ),
                    ClientSession(read_stream, write_stream) as session,
                ):
                    await session.initialize()
                    yield session
            case _:
                raise ValueError(
                    f"unsupported MCP server type for {name}: {server.type}"
                )

    async def list_tools(
        self, name: str, server: AgentMcpServerConfig
    ) -> list[AgentMcpTool]:
        """Page through an upstream server's tool list within the configured call timeout."""

        async def _list_tools() -> list[AgentMcpTool]:
            async with self._session(name, server) as session:
                return await _list_session_tools(session)

        return await asyncio.wait_for(
            _list_tools(), timeout=self.call_timeout_s
        )

    async def call_tool(
        self,
        name: str,
        server: AgentMcpServerConfig,
        tool: str,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invoke an upstream MCP tool and normalize its protocol result for workgate responses."""

        async def _call_tool() -> dict[str, Any]:
            async with self._session(name, server) as session:
                return normalize_tool_result(
                    await session.call_tool(tool, args)
                )

        return await asyncio.wait_for(_call_tool(), timeout=self.call_timeout_s)
