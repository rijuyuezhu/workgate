import asyncio

import pytest

from tests.helpers import build_paired_mcp, mcp_structured
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.remote_worker.dispatch import WorkerDispatcher


def _override_connector_handlers(harness, search, fetch) -> None:
    handlers = dict(harness.executor.dispatcher.handlers)

    async def search_handler(args):
        return await search(str(args["query"]))

    async def fetch_handler(args):
        return await fetch(str(args["id"]))

    handlers["workspace_search"] = search_handler
    handlers["fetch"] = fetch_handler
    harness.executor.dispatcher = WorkerDispatcher(handlers)


@pytest.mark.asyncio
async def test_connector_tools_use_custom_mcp_error_handler(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    async def failing_search(query: str):
        raise ValueError("search failed")

    async def failing_fetch(id: str):
        raise ValueError("fetch failed")

    mcp, harness = build_paired_mcp(get_settings())
    _override_connector_handlers(harness, failing_search, failing_fetch)
    session = mcp_structured(
        await mcp.call_tool("session_start", {"workdir": "."})
    )
    session_id = session["session_id"]

    search_payload = mcp_structured(
        await mcp.call_tool(
            "workspace_search", {"session_id": session_id, "query": "needle"}
        )
    )
    fetch_payload = mcp_structured(
        await mcp.call_tool(
            "fetch", {"session_id": session_id, "id": "notes/demo.txt"}
        )
    )

    assert search_payload == {"results": []}
    assert fetch_payload["id"] == "notes/demo.txt"
    assert fetch_payload["title"] == "notes/demo.txt"
    assert "ValueError: fetch failed" in fetch_payload["text"]
    assert fetch_payload["metadata"]["error"] == "ValueError"


@pytest.mark.asyncio
async def test_connector_tool_timeout_uses_custom_mcp_error_handler(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    async def hanging_search(query: str):
        await asyncio.sleep(5)

    async def hanging_fetch(id: str):
        await asyncio.sleep(5)

    mcp, harness = build_paired_mcp(get_settings())
    _override_connector_handlers(harness, hanging_search, hanging_fetch)
    session = mcp_structured(
        await mcp.call_tool("session_start", {"workdir": "."})
    )
    session_id = session["session_id"]
    monkeypatch.setenv("WORKGATE_TOOL_TIMEOUT_S", "0.01")
    clear_settings_cache()

    search_payload = mcp_structured(
        await mcp.call_tool(
            "workspace_search", {"session_id": session_id, "query": "needle"}
        )
    )
    fetch_payload = mcp_structured(
        await mcp.call_tool(
            "fetch", {"session_id": session_id, "id": "notes/demo.txt"}
        )
    )

    assert search_payload == {"results": []}
    assert fetch_payload["id"] == "notes/demo.txt"
    assert "fetch exceeded 0.01 second tool timeout" in fetch_payload["text"]
    assert fetch_payload["metadata"]["error"] == "PublicToolTimeoutError"
