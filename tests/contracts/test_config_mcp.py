"""MCP tools reuse Control config revision backend and normal OAuth rights."""

import argparse

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from workgate.config.inspection import capture_startup_config
from workgate.config.roles import CONTROL_SETTING_NAMES
from workgate.config.settings import load_settings
from workgate.control.mcp.app import build_mcp
from workgate.oauth.core.context import bind_oauth_claims, reset_oauth_claims
from workgate.oauth.core.scopes import default_scope


@pytest.mark.asyncio
async def test_authorized_model_tools_edit_selected_control_file(tmp_path):
    file = tmp_path / "control.yaml"
    file.write_text("port: 8765\n")
    capture_startup_config(
        "control",
        argparse.Namespace(config=str(file)),
        load_settings(file, setting_names=CONTROL_SETTING_NAMES),
    )
    mcp = build_mcp()
    listed = {tool.name: tool for tool in await mcp.list_tools()}
    inspection_annotations = listed["config_inspect"].annotations
    save_annotations = listed["config_save"].annotations
    assert (
        inspection_annotations is not None
        and inspection_annotations.read_only_hint is True
    )
    assert (
        save_annotations is not None
        and save_annotations.read_only_hint is False
    )
    token = bind_oauth_claims({"scope": default_scope()})
    try:
        inspected = await mcp.call_tool("config_inspect", {})
        assert isinstance(inspected, CallToolResult)
        data = inspected.structured_content
        assert isinstance(data, dict)
        assert data["role"] == "control"
        revision = data["revision"]
        saved = await mcp.call_tool(
            "config_save",
            {
                "revision": revision,
                "changes": {"port": {"op": "set", "value": 9123}},
            },
        )
        assert isinstance(saved, CallToolResult)
        assert isinstance(saved.structured_content, dict)
        assert saved.structured_content["revision"] != revision
        assert "port: 9123" in file.read_text()
    finally:
        reset_oauth_claims(token)


@pytest.mark.asyncio
async def test_model_save_denied_without_write_scope(tmp_path):
    path = tmp_path / "control.yaml"
    path.write_text("port: 8765\n")
    capture_startup_config(
        "control",
        argparse.Namespace(config=str(path)),
        load_settings(path, setting_names=CONTROL_SETTING_NAMES),
    )
    mcp = build_mcp()
    token = bind_oauth_claims({"scope": "shell:read"})
    try:
        with pytest.raises(ToolError, match="shell:write"):
            await mcp.call_tool(
                "config_save",
                {
                    "revision": "a" * 64,
                    "changes": {"port": {"op": "set", "value": 9000}},
                },
            )
    finally:
        reset_oauth_claims(token)
    assert path.read_text() == "port: 8765\n"


@pytest.mark.asyncio
async def test_model_remote_config_inspection_and_save_use_executor_transport(
    monkeypatch,
):
    from types import SimpleNamespace

    from workgate.config.settings import Settings
    from workgate.control.runtime import build_control_runtime

    runtime = build_control_runtime(Settings())
    calls = []
    state = {"reply": {"role": "executor", "status": "running", "fields": []}}

    async def fake_call(executor_id, op, arguments, *, timeout_s):
        calls.append((executor_id, op, arguments, timeout_s))
        return SimpleNamespace(ok=True, result=state["reply"])

    monkeypatch.setattr(runtime.executor_transport, "call", fake_call)
    mcp = build_mcp(runtime=runtime)
    token = bind_oauth_claims({"scope": default_scope()})
    try:
        inspected = await mcp.call_tool(
            "config_inspect", {"executor_id": "exec_demo"}
        )
        assert isinstance(inspected, CallToolResult)
        assert inspected.structured_content == state["reply"]
        assert calls[-1] == ("exec_demo", "ui.config.inspect", {}, 8)

        change = {"max_jobs": {"op": "set", "value": 32}}
        state["reply"] = {"revision": "b" * 64}
        saved = await mcp.call_tool(
            "config_save",
            {
                "executor_id": "exec_demo",
                "revision": "a" * 64,
                "changes": change,
            },
        )
        assert isinstance(saved, CallToolResult)
        assert saved.structured_content == {"revision": "b" * 64}
        assert calls[-1] == (
            "exec_demo",
            "ui.config.save",
            {"revision": "a" * 64, "changes": change},
            12,
        )

        state["reply"] = {"error": "RevisionConflict", "status": 409}
        with pytest.raises(ToolError, match="RevisionConflict"):
            await mcp.call_tool(
                "config_save",
                {
                    "executor_id": "exec_demo",
                    "revision": "a" * 64,
                    "changes": change,
                },
            )
    finally:
        reset_oauth_claims(token)


@pytest.mark.asyncio
async def test_model_remote_config_needs_executor_grant(monkeypatch):
    from workgate.config.settings import Settings
    from workgate.control.runtime import build_control_runtime

    runtime = build_control_runtime(Settings())
    mcp = build_mcp(runtime=runtime)
    token = bind_oauth_claims({"scope": "shell:read shell:write"})
    try:
        with pytest.raises(ToolError, match="executor:use"):
            await mcp.call_tool("config_inspect", {"executor_id": "exec_demo"})
        with pytest.raises(ToolError, match="executor:use"):
            await mcp.call_tool(
                "config_save",
                {
                    "executor_id": "exec_demo",
                    "revision": "a" * 64,
                    "changes": {"max_jobs": {"op": "set", "value": 32}},
                },
            )
    finally:
        reset_oauth_claims(token)
