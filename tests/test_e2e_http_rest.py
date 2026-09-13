import httpx
import pytest

from tests.e2e_helpers import RestToolClient, run_http_process
from tests.e2e_scenarios import (
    assert_core_tool_surface,
    exercise_environment_tool,
    exercise_explicit_session_workflow,
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
async def test_http_rest_process_exercises_core_tool_categories(tmp_path):
    async with run_http_process(tmp_path, mode="http") as (base_url, workspace):
        client = RestToolClient(base_url)

        await assert_core_tool_surface(client)
        await exercise_environment_tool(client, workspace)
        await exercise_explicit_session_workflow(client, workspace)
        await exercise_filesystem_and_search_tools(client, workspace)
        await exercise_session_copy_tool(client, workspace)
        await exercise_workspace_connector_tools(client)
        await exercise_shell_tools(client, workspace)
        await exercise_session_bound_job_tools(client, workspace)
        await exercise_interactive_shell_tools(client)
        await exercise_todo_tools(client)


@pytest.mark.asyncio
async def test_http_rest_process_exercises_file_download_links(tmp_path):
    async with run_http_process(tmp_path, mode="http") as (base_url, workspace):
        client = RestToolClient(base_url)
        payload = b"download-payload-\x00-binary"
        (workspace / "artifact.bin").write_bytes(payload)
        session = await client.call_tool("session_start", {"workdir": "."})
        session_id = session["session_id"]

        link = await client.call_tool(
            "create_file_link",
            {
                "session_id": session_id,
                "path": "artifact.bin",
                "ttl_s": 60,
                "filename": "result.bin",
                "max_downloads": 1,
            },
        )
        assert link["url"].startswith(f"{base_url}/download/")

        async with httpx.AsyncClient(
            timeout=10, trust_env=False
        ) as http_client:
            response = await http_client.get(link["url"])
            assert response.status_code == 200
            assert response.content == payload
            assert "result.bin" in response.headers["content-disposition"]

            exhausted = await http_client.get(link["url"])
            assert exhausted.status_code == 410

        listed = await client.call_tool(
            "list_file_links", {"session_id": session_id}
        )
        assert listed == {"links": []}

        second = await client.call_tool(
            "create_file_link",
            {"session_id": session_id, "path": "artifact.bin", "ttl_s": 60},
        )
        revoked = await client.call_tool(
            "revoke_file_link",
            {"session_id": session_id, "link_id": second["link_id"]},
        )
        assert revoked == {"revoked": True, "link_id": second["link_id"]}

        async with httpx.AsyncClient(
            timeout=10, trust_env=False
        ) as http_client:
            missing = await http_client.get(second["url"])
        assert missing.status_code == 404
