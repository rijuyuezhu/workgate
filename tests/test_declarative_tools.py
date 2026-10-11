from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from workgate.oauth.core.context import (
    bind_oauth_claims,
    require_oauth_scopes,
    reset_oauth_claims,
)
from workgate.tools.declarative import DeclarativeToolRegistry, ToolDefinition


def test_registry_rejects_duplicate_tool_names():
    class SampleRegistry(DeclarativeToolRegistry):
        pass

    definition = ToolDefinition(
        func=_sample_tool,
        name="sample_tool",
    )
    SampleRegistry.register_tool(definition)
    with pytest.raises(
        ValueError, match="Duplicate tool definition: sample_tool"
    ):
        SampleRegistry.register_tool(definition)


def _sample_context():
    from workgate.config.control import resolve_control_config
    from workgate.config.settings import Settings
    from workgate.tools.contracts import McpToolContext

    return McpToolContext(
        settings=resolve_control_config(Settings()),
    )


async def _sample_tool() -> dict:
    return {}


class _FakeMcp:
    def __init__(self) -> None:
        self.handler = None

    def tool(self, **kwargs):
        del kwargs

        def decorator(handler):
            self.handler = handler
            return handler

        return decorator


@pytest.mark.asyncio
async def test_mcp_handler_enforces_required_oauth_scopes():
    async def sample_tool() -> dict:
        return {"ok": True}

    definition = ToolDefinition(
        func=sample_tool,
        name="sample_tool",
        oauth_scopes=("shell:read", "shell:execute"),
    )
    mcp = _FakeMcp()

    definition.register_mcp(cast(Any, mcp), _sample_context())
    assert mcp.handler is not None
    handler = cast(Callable[[], Awaitable[dict[str, bool]]], mcp.handler)

    claims_token = bind_oauth_claims({"scope": "shell:read"})
    try:
        with pytest.raises(
            ToolError, match="Missing required OAuth scope: shell:execute"
        ):
            await handler()
    finally:
        reset_oauth_claims(claims_token)

    claims_token = bind_oauth_claims({"scope": "shell:read shell:execute"})
    try:
        assert await handler() == {"ok": True}
    finally:
        reset_oauth_claims(claims_token)


@pytest.mark.asyncio
async def test_explicit_mcp_text_projection_preserves_canonical_structured_result():
    async def sample_tool() -> dict[str, object]:
        return {"kind": "file", "content": "compact"}

    definition = ToolDefinition(
        func=sample_tool,
        name="read",
    )

    mcp = _FakeMcp()
    definition.register_mcp(cast(Any, mcp), _sample_context())
    assert mcp.handler is not None
    handler = cast(Callable[[], Awaitable[CallToolResult]], mcp.handler)
    rendered = await handler()
    assert rendered.structured_content == {
        "kind": "file",
        "content": "compact",
    }
    assert isinstance(rendered.content[0], TextContent)
    assert rendered.content[0].text == "compact"


@pytest.mark.asyncio
async def test_dynamic_oauth_scope_failures_use_standard_tool_error_shape():
    async def sample_tool() -> dict[str, bool]:
        require_oauth_scopes(("shell:execute",))
        return {"ok": True}

    definition = ToolDefinition(
        func=sample_tool,
        name="sample_tool",
        oauth_scopes=("shell:read",),
    )
    claims_token = bind_oauth_claims({"scope": "shell:read"})
    try:
        mcp = _FakeMcp()
        definition.register_mcp(cast(Any, mcp), _sample_context())
        assert mcp.handler is not None
        handler = cast(Callable[[], Awaitable[dict[str, bool]]], mcp.handler)
        with pytest.raises(
            ToolError, match="Missing required OAuth scope: shell:execute"
        ):
            await handler()
    finally:
        reset_oauth_claims(claims_token)


def test_tool_definition_rejects_unknown_annotations():
    definition = ToolDefinition(
        func=_sample_tool,
        name="sample_tool",
        annotations="future-annotation",  # type: ignore[arg-type]
    )

    with pytest.raises(
        ValueError, match="Invalid annotations: future-annotation"
    ):
        definition._mcp_annotations()


@pytest.mark.asyncio
async def test_session_termination_remains_model_visible_on_mcp_v2():
    from workgate.errors import SessionTerminationRequestedError

    async def stopped(session_id: str) -> str:
        raise SessionTerminationRequestedError(session_id)

    mcp = MCPServer("termination-test")
    ToolDefinition(func=stopped, name="stopped").register_mcp(
        mcp, _sample_context()
    )
    with pytest.raises(
        ToolError, match="Stop immediately.*Do not perform any further work"
    ):
        await mcp.call_tool(
            "stopped", {"session_id": "sess_123456789123456789123456789"}
        )


@pytest.mark.asyncio
async def test_agent_tool_arbitrary_value_error_is_not_exposed():
    async def call_agent_mcp_tool(server: str) -> str:
        raise ValueError("private secret value from unexpected code path")

    mcp = MCPServer("safe-error-test")
    ToolDefinition(
        func=call_agent_mcp_tool,
        name="call_agent_mcp_tool",
    ).register_mcp(mcp, _sample_context())
    with pytest.raises(
        ToolError, match="^Error executing tool call_agent_mcp_tool$"
    ):
        await mcp.call_tool("call_agent_mcp_tool", {"server": "docs"})


@pytest.mark.asyncio
async def test_mcp_enforced_scope_explains_denial_to_client():
    async def protected() -> str:
        return "not executed"

    mcp = MCPServer("scope-test")
    ToolDefinition(
        func=protected,
        name="protected",
        oauth_scopes=("shell:read", "shell:write"),
    ).register_mcp(mcp, _sample_context())
    token = bind_oauth_claims({"scope": "shell:read"})
    try:
        with pytest.raises(
            ToolError, match="Missing required OAuth scope: shell:write"
        ):
            await mcp.call_tool("protected", {})
    finally:
        reset_oauth_claims(token)
