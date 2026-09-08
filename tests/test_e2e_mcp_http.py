import base64

import httpx
import pytest
from mcp.types import ImageContent

from tests.e2e_helpers import run_http_process, streamable_http_tool_client
from tests.e2e_scenarios import (
    assert_core_tool_surface,
    exercise_environment_tool,
    exercise_explicit_session_workflow,
    exercise_file_download_links,
    exercise_filesystem_and_search_tools,
    exercise_interactive_shell_tools,
    exercise_session_bound_job_tools,
    exercise_session_copy_tool,
    exercise_shell_tools,
    exercise_todo_tools,
    exercise_workspace_connector_tools,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_mcp_streamable_http_process_exercises_core_tool_categories(
    tmp_path,
):
    async with (
        run_http_process(tmp_path, mode="mcp") as (base_url, workspace),
        streamable_http_tool_client(base_url) as client,
        httpx.AsyncClient(base_url=base_url, trust_env=False) as http_client,
    ):
        page = await http_client.get("/ui")
        bootstrap = await http_client.get("/api/ui/bootstrap")

        assert page.status_code == 200
        assert bootstrap.status_code == 200

        await assert_core_tool_surface(client)
        await exercise_environment_tool(client, workspace)
        await exercise_explicit_session_workflow(client, workspace)
        await exercise_filesystem_and_search_tools(client, workspace)
        await exercise_session_copy_tool(client, workspace)
        await exercise_workspace_connector_tools(client)
        await exercise_file_download_links(client, base_url, workspace)
        await exercise_shell_tools(client, workspace)
        await exercise_session_bound_job_tools(client, workspace)
        await exercise_interactive_shell_tools(client)
        await exercise_todo_tools(client)


@pytest.mark.asyncio
async def test_mcp_streamable_http_returns_native_image_content(tmp_path):
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lP7LAAAAAElFTkSuQmCC"
    )
    async with (
        run_http_process(tmp_path, mode="mcp") as (base_url, workspace),
        streamable_http_tool_client(base_url) as client,
    ):
        (workspace / "pixel.png").write_bytes(png)
        session = await client.call_tool("session_start", {"workdir": "."})

        result = await client.call_tool_result(
            "view_image",
            {"session_id": session["session_id"], "path": "pixel.png"},
        )

        assert result.isError is False
        assert isinstance(result.content[0], ImageContent)
        assert base64.b64decode(result.content[0].data) == png
        assert result.structuredContent is not None
        assert result.structuredContent["session_id"] == session["session_id"]
        assert result.structuredContent["mime_type"] == "image/png"
        assert result.structuredContent["bytes"] == len(png)
