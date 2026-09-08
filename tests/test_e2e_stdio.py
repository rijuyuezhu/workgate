import pytest

from tests.e2e_helpers import stdio_tool_client
from tests.e2e_scenarios import assert_core_tool_surface

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_stdio_process_has_no_hidden_local_execution_without_executor(
    tmp_path,
):
    async with stdio_tool_client(tmp_path) as (client, _workspace):
        await assert_core_tool_surface(client)

        result = await client.call_tool_result(
            "session_start", {"workdir": "."}
        )
        assert result.isError is True
        assert result.content
        error_text = getattr(result.content[0], "text", "")
        assert "requires exactly one eligible executor" in error_text


@pytest.mark.asyncio
async def test_stdio_process_uses_sdk_unknown_tool_error(tmp_path):
    async with stdio_tool_client(tmp_path) as (client, _workspace):
        removed_name = "remote_run_shell_tool"
        assert removed_name not in await client.list_tools()

        result = await client.call_tool_result(
            removed_name, {"machine": "worker"}
        )

        assert result.isError is True
        assert result.structuredContent is None
        assert len(result.content) == 1
        error_text = getattr(result.content[0], "text", "")
        assert error_text == f"Unknown tool: {removed_name}"
        assert "replacement" not in error_text.lower()
        assert "refresh" not in error_text.lower()
        assert "session_start" not in error_text.lower()
