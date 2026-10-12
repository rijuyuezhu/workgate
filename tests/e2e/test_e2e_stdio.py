import pytest

from tests.e2e.helpers import stdio_tool_client
from tests.e2e.scenarios import assert_core_tool_surface
from workgate import __version__

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_stdio_process_has_no_hidden_local_execution_without_executor(
    tmp_path,
):
    async with stdio_tool_client(tmp_path) as (client, _workspace):
        assert client.server_version == __version__
        await assert_core_tool_surface(client)

        result = await client.call_tool_result(
            "session_start", {"workdir": "."}
        )
        assert result.is_error is True
        assert result.content
        error_text = getattr(result.content[0], "text", "")
        assert "requires exactly one eligible executor" in error_text


@pytest.mark.asyncio
async def test_stdio_process_uses_sdk_unknown_tool_error(tmp_path):
    async with stdio_tool_client(tmp_path) as (client, _workspace):
        unknown_name = "no_such_workgate_tool"
        assert unknown_name not in await client.list_tools()

        result = await client.call_tool_result(unknown_name, {})

        assert result.is_error is True
        assert result.structured_content is None
        assert len(result.content) == 1
        error_text = getattr(result.content[0], "text", "")
        assert error_text == f"Unknown tool: {unknown_name}"
