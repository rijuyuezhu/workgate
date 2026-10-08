"""Bounded progressive disclosure of external MCP tool metadata."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from workgate.agent_bridge.discovery import McpDiscovery
from workgate.agent_bridge.mcp import (
    MAX_SERVER_DESCRIPTOR_BYTES,
    MAX_TOOL_DESCRIPTOR_BYTES,
    MAX_TOOLS_PER_SERVER,
    _list_session_tools,
)


@pytest.mark.asyncio
async def test_search_excludes_schema_and_inspect_reuses_cached_metadata() -> (
    None
):
    calls: list[str | None] = []

    async def fetch(session_id: str | None) -> list[dict[str, Any]]:
        calls.append(session_id)
        return [
            {
                "server": "docs",
                "tool": "find",
                "description": "Search documentation",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            },
            {
                "server": "git",
                "tool": "status",
                "description": "Show repository status",
                "input_schema": {},
            },
        ]

    catalog = McpDiscovery(fetch)
    found = await catalog.search("search", session_id="one")
    assert found["tools"] == [
        {
            "server": "docs",
            "tool": "find",
            "description": "Search documentation",
        }
    ]
    assert "input_schema" not in str(found)
    inspected = await catalog.inspect("docs", "find", session_id="one")
    assert inspected["input_schema"]["properties"]["query"] == {
        "type": "string"
    }
    assert calls == ["one"]
    assert (await catalog.search("", session_id="two"))["total_matches"] == 2
    assert calls == ["one", "two"]
    await catalog.search("", session_id="one", refresh=True)
    assert calls == ["one", "two", "one"]
    catalog.invalidate("one")
    await catalog.inspect("git", "status", session_id="one")
    assert len(calls) == 4
    with pytest.raises(ValueError, match="Unknown agent MCP tool"):
        await catalog.inspect("docs", "missing", session_id="one")


@pytest.mark.asyncio
async def test_search_rejects_invalid_limits_and_bounded_catalogs() -> None:
    async def fetch(_session_id: str | None) -> list[dict[str, Any]]:
        return [
            {"server": "s", "tool": "t", "description": "x", "input_schema": {}}
        ]

    catalog = McpDiscovery(fetch)
    for value in (0, 51):
        with pytest.raises(ValueError, match="limit"):
            await catalog.search("", limit=value)

    async def oversized(_session_id: str | None) -> list[dict[str, Any]]:
        return [
            {
                "server": "s",
                "tool": "t",
                "description": "x" * 18000,
                "input_schema": {},
            }
        ]

    with pytest.raises(ValueError, match="size limit"):
        await McpDiscovery(oversized).search("")

    async def too_many(_session_id: str | None) -> list[dict[str, Any]]:
        return [
            {
                "server": "s",
                "tool": f"t{i}",
                "description": "x",
                "input_schema": {},
            }
            for i in range(MAX_TOOLS_PER_SERVER + 1)
        ]

    with pytest.raises(ValueError, match="bounds"):
        await McpDiscovery(too_many).search("")


@pytest.mark.asyncio
async def test_upstream_tool_pagination_is_bounded_before_caching() -> None:
    class FakeSession:
        def __init__(self, tools: list[dict[str, Any]]) -> None:
            self.tools = tools

        async def list_tools(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(tools=self.tools, nextCursor=None)

    good = [{"name": "ping", "description": "ok", "inputSchema": {}}]
    assert len(await _list_session_tools(cast(Any, FakeSession(good)))) == 1
    with pytest.raises(ValueError, match="count"):
        await _list_session_tools(
            cast(Any, FakeSession(good * (MAX_TOOLS_PER_SERVER + 1)))
        )
    with pytest.raises(ValueError, match="descriptor"):
        await _list_session_tools(
            cast(
                Any,
                FakeSession(
                    [
                        {
                            "name": "huge",
                            "description": "x" * MAX_TOOL_DESCRIPTOR_BYTES,
                            "inputSchema": {},
                        }
                    ]
                ),
            )
        )
    with pytest.raises(ValueError, match="catalog"):
        await _list_session_tools(
            cast(
                Any,
                FakeSession(
                    [
                        {
                            "name": f"a{i}",
                            "description": "x" * 8000,
                            "inputSchema": {},
                        }
                        for i in range(MAX_SERVER_DESCRIPTOR_BYTES // 8000 + 1)
                    ]
                ),
            )
        )


@pytest.mark.asyncio
async def test_discovery_server_filter_scope_eviction_and_server_bound() -> (
    None
):
    scopes: list[str | None] = []

    async def fetch(session_id: str | None) -> list[dict[str, Any]]:
        scopes.append(session_id)
        return [
            {
                "server": "one",
                "tool": "ping",
                "description": "one",
                "input_schema": {},
            },
            {
                "server": "two",
                "tool": "status",
                "description": "two",
                "input_schema": {},
            },
        ]

    catalog = McpDiscovery(fetch)
    selected = await catalog.search("", server="two", session_id="scope0")
    assert selected["tools"] == [
        {"server": "two", "tool": "status", "description": "two"}
    ]
    for i in range(1, 17):
        await catalog.search("", session_id=f"scope{i}")
    assert len(catalog._cache) == 16
    assert "scope0" not in catalog._cache
    await catalog.search("", session_id="scope0")
    assert scopes.count("scope0") == 2

    async def many_servers(_session_id: str | None) -> list[dict[str, Any]]:
        return [
            {
                "server": f"server{i}",
                "tool": "ping",
                "description": "",
                "input_schema": {},
            }
            for i in range(33)
        ]

    with pytest.raises(ValueError, match="discovery catalog exceeds bounds"):
        await McpDiscovery(many_servers).search("")
