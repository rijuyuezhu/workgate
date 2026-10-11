"""Debug HTTP is only a JSON wrapper over the registered MCP tool surface."""

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextContent

from tests.support.paired import build_paired_http_app
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.http.app import build_http_app
from workgate.control.mcp.app import build_mcp


@pytest.fixture
def debug_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    return TestClient(build_http_app())


def test_discovery_is_exact_mcp_registration_and_hides_transfer(debug_client):
    discovery = debug_client.get("/tools")
    assert discovery.status_code == 200
    assert discovery.headers["Cache-Control"] == "no-store"
    tools = {tool["name"]: tool for tool in discovery.json()["tools"]}
    assert "read" in tools
    assert "version" in tools
    assert tools["read"]["inputSchema"]["properties"]["path"]
    assert "transfer_stat" not in tools
    assert "transfer_read_chunk" not in tools
    assert "transfer_pack_dir" not in tools
    unknown = debug_client.post("/tools/transfer_stat", json={}).json()
    assert unknown["isError"] is True
    assert "Unknown tool" in unknown["content"][0]["text"]


def test_debug_tools_have_only_one_post_route(debug_client):
    assert debug_client.get("/tools/version").status_code == 405
    assert debug_client.get("/tools/todo").status_code == 405
    assert debug_client.post("/tools/todo", json={}).json()["isError"] is True
    assert (
        debug_client.post("/tools/file_link/create", json={}).status_code == 404
    )


def test_debug_result_and_validation_use_mcp_wire_shape(debug_client):
    success = debug_client.post("/tools/version", json={})
    assert success.status_code == 200
    result = CallToolResult.model_validate(success.json())
    assert result.is_error is False
    assert result.structured_content is not None
    assert "version" in result.structured_content

    # MCP calls accept omitted arguments; the debug wrapper has the same default.
    missing_body = debug_client.post("/tools/version")
    assert missing_body.status_code == 200
    assert missing_body.json() == success.json()

    invalid = debug_client.post("/tools/read", json={})
    assert invalid.status_code == 200
    failure = CallToolResult.model_validate(invalid.json())
    assert failure.is_error is True
    assert isinstance(failure.content[0], TextContent)
    assert "session_id" in failure.content[0].text


def test_unknown_tool_does_not_have_second_registry(debug_client):
    result = debug_client.post("/tools/not_registered", json={})
    assert result.status_code == 200
    assert result.json()["isError"] is True
    assert result.json()["content"][0]["text"] == "Unknown tool: not_registered"


@pytest.mark.asyncio
async def test_debug_and_direct_mcp_return_identical_tool_payload(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    debug = TestClient(build_http_app()).post("/tools/version", json={}).json()
    direct = (await build_mcp().call_tool("version", {})).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    # Version reports stable runtime data; both transports use the same MCP model.
    assert debug == direct


def test_debug_session_call_uses_bound_control_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    app, harness = build_paired_http_app(get_settings())
    with TestClient(app) as client:
        opened = client.post(
            "/tools/session_start", json={"workdir": "."}
        ).json()
        assert not opened["isError"]
        session_id = opened["structuredContent"]["session_id"]
        result = client.post(
            "/tools/list_files", json={"session_id": session_id, "path": "."}
        ).json()
        assert not result["isError"]
        assert result["structuredContent"] is not None


def test_late_mcp_tool_automatically_appears_in_debug_http(
    tmp_path, monkeypatch
):
    """No second route or handler registration is needed for a new MCP tool."""
    from workgate.tools.catalog import ToolCatalog
    from workgate.tools.contracts import ToolRegistry

    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    clear_settings_cache()
    holder = []

    class DynamicRegistry(ToolRegistry):
        def register_mcp(self, mcp, context):
            holder.append(mcp)

    app = build_http_app(tool_catalog=ToolCatalog((DynamicRegistry(),)))
    with TestClient(app) as client:
        assert client.get("/tools").json()["tools"] == []

        @holder[0].tool(name="late_example")
        async def example(message: str) -> dict[str, str]:
            return {"message": message}

        assert {
            tool["name"] for tool in client.get("/tools").json()["tools"]
        } == {"late_example"}
        reply = client.post(
            "/tools/late_example", json={"message": "registered"}
        )
        assert reply.status_code == 200
        assert reply.json()["structuredContent"] == {"message": "registered"}
        assert reply.json()["isError"] is False


def test_debug_errors_distinguish_tool_failures_from_protocol_errors(
    tmp_path, monkeypatch
):
    """Use SDK error results for tools and SDK ErrorData for protocol errors."""
    from workgate.tools.catalog import ToolCatalog
    from workgate.tools.contracts import ToolRegistry

    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    clear_settings_cache()

    class ErrorRegistry(ToolRegistry):
        def register_mcp(self, mcp, context):
            @mcp.tool(name="expected_error")
            async def expected() -> str:
                raise ToolError("safe diagnostic")

            @mcp.tool(name="unexpected_error")
            async def unexpected() -> str:
                raise ValueError("secret-like internal diagnostic")

            @mcp.tool(name="protocol_error")
            async def protocol() -> str:
                raise MCPError(
                    -32602, "Invalid protocol parameters", {"reason": "bad"}
                )

    with TestClient(
        build_http_app(tool_catalog=ToolCatalog((ErrorRegistry(),)))
    ) as client:
        expected_response = client.post("/tools/expected_error", json={})
        assert expected_response.status_code == 200
        expected_result = CallToolResult.model_validate(
            expected_response.json()
        )
        assert expected_result.is_error is True
        assert isinstance(expected_result.content[0], TextContent)
        assert "safe diagnostic" in expected_result.content[0].text

        unexpected_response = client.post("/tools/unexpected_error", json={})
        assert unexpected_response.status_code == 200
        unexpected_result = CallToolResult.model_validate(
            unexpected_response.json()
        )
        assert unexpected_result.is_error is True
        assert "secret-like internal diagnostic" not in str(unexpected_result)

        protocol_response = client.post("/tools/protocol_error", json={})
        assert protocol_response.status_code == 400
        assert protocol_response.json() == {
            "code": -32602,
            "message": "Invalid protocol parameters",
            "data": {"reason": "bad"},
        }


def test_duplicate_mcp_tool_registration_fails_closed(tmp_path, monkeypatch):
    """MCP and debug HTTP never silently choose one of two tool definitions."""
    from workgate.tools.catalog import ToolCatalog
    from workgate.tools.contracts import ToolRegistry

    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    clear_settings_cache()

    class FirstRegistry(ToolRegistry):
        def register_mcp(self, mcp, context):
            @mcp.tool(name="collision")
            async def first() -> str:
                return "first"

    class SecondRegistry(ToolRegistry):
        def register_mcp(self, mcp, context):
            @mcp.tool(name="collision")
            async def second() -> str:
                return "second"

    with pytest.raises(ValueError, match="Duplicate MCP tool: collision"):
        build_http_app(
            tool_catalog=ToolCatalog((FirstRegistry(), SecondRegistry()))
        )
    with pytest.raises(ValueError, match="Duplicate MCP tool: collision"):
        build_mcp(tool_catalog=ToolCatalog((FirstRegistry(), SecondRegistry())))
