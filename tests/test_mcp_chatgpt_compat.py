import base64
import hashlib
import json
import os
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette

from tests.helpers import mcp_structured
from workgate.agent_bridge.mcp import AgentMcpTool
from workgate.app_paths import app_paths
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.http.app import build_http_app
from workgate.control.mcp.app import (
    _add_public_routes_to_mcp_http_app,
    build_mcp,
)
from workgate.control.mcp.transport_security import (
    transport_security_settings,
)
from workgate.oauth.core import service as oauth_service
from workgate.oauth.core.client_store import client_store_path
from workgate.oauth.core.models import AuthCode, OAuthClient
from workgate.oauth.core.scopes import supported_scopes
from workgate.oauth.core.service import _prune_clients, _prune_codes
from workgate.oauth.core.state import (
    OAuthState,
    configure_oauth_state,
    oauth_state,
)
from workgate.oauth.core.urls import resource_url
from workgate.oauth.http.authorization import _authorize_form
from workgate.oauth.http.responses import oauth_redirect
from workgate.oauth.protocol.token_codec import (
    issue_access_token,
    validate_bearer_token,
)
from workgate.tools.registry import agent as tools_module


def _output_schema(tool: Any) -> dict[str, Any]:
    schema = tool.outputSchema
    assert schema is not None
    return schema


def _s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@pytest.fixture(autouse=True)
def _oauth_state_owner(tmp_path):
    state = OAuthState(tmp_path / ".oauth-state")
    previous = configure_oauth_state(state)
    try:
        yield state
    finally:
        configure_oauth_state(previous)


def test_oauth_supported_scopes_include_feature_scopes():
    assert supported_scopes() == [
        "shell:read",
        "shell:write",
        "shell:execute",
        "git:write",
        "file:share",
        "remote:use",
        "audit:read",
        "audit:full",
    ]


def test_oauth_resource_defaults_to_mcp_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://workgate.example.com")
    monkeypatch.delenv("WORKGATE_OAUTH_RESOURCE", raising=False)
    clear_settings_cache()

    assert resource_url() == "https://workgate.example.com/mcp"


def test_oauth_urls_ignore_untrusted_request_host_headers(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_OAUTH_ADMIN_PIN", "1234")
    monkeypatch.delenv("WORKGATE_BASE_URL", raising=False)
    monkeypatch.delenv("WORKGATE_OAUTH_ISSUER", raising=False)
    monkeypatch.delenv("WORKGATE_OAUTH_RESOURCE", raising=False)
    clear_settings_cache()
    oauth_state().clients.clear()
    oauth_state().codes.clear()

    headers = {
        "host": "attacker.example",
        "x-forwarded-host": "forwarded-attacker.example",
        "x-forwarded-proto": "https",
    }
    client = TestClient(
        _add_public_routes_to_mcp_http_app(Starlette())[0],
        base_url="https://attacker.example",
    )

    metadata = client.get(
        "/.well-known/oauth-protected-resource/mcp", headers=headers
    )
    assert metadata.status_code == 200
    assert metadata.json()["resource"] == "http://127.0.0.1:8765/mcp"
    assert metadata.json()["authorization_servers"] == ["http://127.0.0.1:8765"]

    register = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://client.example/callback"]},
        headers=headers,
    ).json()
    verifier = "h" * 64
    authorize = client.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": register["client_id"],
            "redirect_uri": "https://client.example/callback",
            "resource": "http://127.0.0.1:8765/mcp",
            "code_challenge": _s256_challenge(verifier),
            "code_challenge_method": "S256",
            "pin": "1234",
        },
        headers=headers,
        follow_redirects=False,
    )
    assert authorize.status_code == 302
    redirect_query = parse_qs(urlparse(authorize.headers["location"]).query)
    assert redirect_query["iss"] == ["http://127.0.0.1:8765"]

    token_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": redirect_query["code"][0],
            "client_id": register["client_id"],
            "redirect_uri": "https://client.example/callback",
            "resource": "http://127.0.0.1:8765/mcp",
            "code_verifier": verifier,
        },
        headers=headers,
    )
    assert token_response.status_code == 200
    claims = validate_bearer_token(token_response.json()["access_token"])
    assert claims["iss"] == "http://127.0.0.1:8765"
    assert claims["aud"] == "http://127.0.0.1:8765/mcp"


@pytest.mark.asyncio
async def test_mcp_metadata_for_chatgpt_developer_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://workgate.example.com")
    clear_settings_cache()

    mcp = build_mcp()
    assert mcp.instructions is not None
    assert "You are a coding agent aiming to help the user" in mcp.instructions
    assert "Do not commit, push, amend, create PRs, release" in mcp.instructions
    assert "secret_scan is heuristic" in mcp.instructions
    assert (
        "`session_id` identifies the agent/workspace session"
        in mcp.instructions
    )
    assert "`bash(async_=true)` returns a `job_id`" in mcp.instructions
    assert "`bash(pty=true)` is local-session only" in mcp.instructions
    assert "Do not use `shell_id` with `job`" in mcp.instructions

    transport_security = mcp.settings.transport_security
    assert transport_security is not None
    assert "workgate.example.com" in transport_security.allowed_hosts
    assert "workgate.example.com:443" in transport_security.allowed_hosts
    assert "workgate.example.com:*" not in transport_security.allowed_hosts

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    search_meta = tools["workspace_search"].meta
    session_meta = tools["session_start"].meta
    assert "environment_info" not in tools
    assert "remote" not in tools
    assert search_meta is not None
    assert session_meta is not None
    assert search_meta["securitySchemes"][0]["type"] == "noauth"
    assert search_meta["securitySchemes"][1]["scopes"] == ["shell:read"]
    assert session_meta["securitySchemes"][0]["type"] == "oauth2"
    assert session_meta["securitySchemes"][0]["scopes"] == ["shell:read"]

    def tool_oauth_scopes(name: str) -> list[str]:
        meta = tools[name].meta
        assert meta is not None
        return meta["securitySchemes"][0]["scopes"]

    assert tool_oauth_scopes("bash") == [
        "shell:read",
        "shell:execute",
    ]
    assert tool_oauth_scopes("write_file") == [
        "shell:read",
        "shell:write",
    ]
    assert tool_oauth_scopes("create_file_link") == [
        "shell:read",
        "file:share",
    ]
    assert tool_oauth_scopes("remote_admin") == ["remote:use"]
    assert tool_oauth_scopes("audit_tail") == ["audit:read"]
    assert all(tool.outputSchema is not None for tool in tools.values())
    bash_schema = tools["bash"].outputSchema
    assert bash_schema is not None
    assert bash_schema["title"] == "ShellExecutionOutput"
    assert set(bash_schema["properties"]) >= {
        "mode",
        "command",
        "cwd",
        "result",
    }
    assert "session_id" in tools["bash"].inputSchema["required"]
    assert "session_id" in tools["run_python_code"].inputSchema["required"]
    assert "session_id" in tools["tree_view"].inputSchema["required"]
    assert "session_id" in tools["glob_search"].inputSchema["required"]
    assert "session_id" in tools["job"].inputSchema["required"]
    search_schema = tools["search"].outputSchema
    assert search_schema is not None
    assert "matches" in search_schema["properties"]
    assert "numbered_content" in search_schema["properties"]
    fetch_schema = tools["fetch"].outputSchema
    assert fetch_schema is not None
    assert set(fetch_schema["properties"]) == {
        "id",
        "title",
        "text",
        "url",
        "metadata",
    }
    workspace_search_description = (
        tools["workspace_search"].description or ""
    ).lower()
    fetch_description = (tools["fetch"].description or "").lower()
    assert {"search", "fetch"} <= set(workspace_search_description.split())
    assert "workspace_search" in fetch_description
    assert "read" in fetch_description and "session_id" in fetch_description

    structured = mcp_structured(
        await mcp.call_tool("session_start", {"workdir": "."})
    )
    assert re.fullmatch(r"[A-Za-z0-9]{8}", structured["session_id"])
    assert structured["target"] == "local"
    assert structured["workdir"] == str(tmp_path)
    assert structured["workspace_root"] == str(tmp_path)
    assert "session_id" in structured["message"]


@pytest.mark.asyncio
async def test_shell_tool_schema_exposes_session_and_execution_modes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    tool = {tool.name: tool for tool in await build_mcp().list_tools()}["bash"]

    output_schema = _output_schema(tool)
    command_input = tool.inputSchema["properties"]["command"]
    session_input = tool.inputSchema["properties"]["session_id"]
    timeout_input = tool.inputSchema["properties"]["timeout_s"]
    mode_output = output_schema["properties"]["mode"]
    result_output = output_schema["properties"]["result"]

    assert "session_id" in tool.inputSchema["required"]
    assert command_input["type"] == "string"
    assert session_input["type"] == "string"
    assert timeout_input["default"] is None
    assert mode_output["type"] == "string"
    assert result_output["type"] == "object"
    description = tool.description or ""
    for concept in ("session_id", "job_id", "shell_id", "async_", "pty"):
        assert concept in description


@pytest.mark.asyncio
async def test_persistent_shell_tools_use_shell_id_not_session_id(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    tools = {tool.name: tool for tool in await build_mcp().list_tools()}
    companion_names = [
        "send_persistent_shell_input",
        "resize_persistent_shell",
        "read_persistent_shell_output",
        "kill_persistent_shell",
    ]
    for name in companion_names:
        tool = tools[name]
        input_properties = tool.inputSchema["properties"]
        output_schema = _output_schema(tool)

        assert "shell_id" in tool.inputSchema["required"]
        assert "shell_id" in input_properties
        assert "session_id" not in input_properties
        assert "shell_id" in output_schema["properties"]
        assert "session_id" not in output_schema["properties"]

    list_schema = _output_schema(tools["list_persistent_shells"])
    assert "shells" in list_schema["properties"]
    assert "sessions" not in list_schema["properties"]
    assert "shell_id" in str(list_schema)
    assert "session_id" not in str(list_schema)


@pytest.mark.asyncio
async def test_shell_tool_returns_per_tool_structured_content(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    mcp = build_mcp()
    session = mcp_structured(
        await mcp.call_tool("session_start", {"workdir": "."})
    )
    structured = mcp_structured(
        await mcp.call_tool(
            "bash",
            {"session_id": session["session_id"], "command": "echo ok"},
        )
    )

    assert structured["mode"] == "command"
    assert structured["command"] == "echo ok"
    assert structured["result"]["stdout"].splitlines() == ["ok"]
    assert "data" not in structured


@pytest.mark.asyncio
async def test_file_tool_schema_exposes_grounded_read_contract(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    tools = {tool.name: tool for tool in await build_mcp().list_tools()}
    read_tool = tools["read"]
    list_files_tool = tools["list_files"]

    read_output_schema = _output_schema(read_tool)
    list_files_output_schema = _output_schema(list_files_tool)
    assert read_output_schema["title"] == "ReadOutput"
    assert list_files_output_schema["title"] == "ListFilesOutput"
    assert (
        "selector suffix"
        in read_tool.inputSchema["properties"]["path"]["description"]
    )
    assert "binary_preview" not in read_tool.inputSchema["properties"]
    assert "binary_preview_bytes" not in read_tool.inputSchema["properties"]
    assert "file" in read_output_schema["properties"]
    assert "directory" in read_output_schema["properties"]
    assert "content" in read_output_schema["properties"]
    assert "entries" in list_files_output_schema["properties"]


@pytest.mark.asyncio
async def test_search_tool_schema_exposes_search_and_paging_contract(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    tools = {tool.name: tool for tool in await build_mcp().list_tools()}
    search_tool = tools["search"]
    tree_tool = tools["tree_view"]
    glob_tool = tools["glob_search"]

    search_output_schema = _output_schema(search_tool)
    tree_output_schema = _output_schema(tree_tool)
    assert search_output_schema["title"] == "GrepSearchOutput"
    assert tree_output_schema["title"] == "TreeViewOutput"
    pattern_description = search_tool.inputSchema["properties"]["pattern"][
        "description"
    ].lower()
    assert "regular expression" in pattern_description
    assert (
        "case-sensitive"
        in search_tool.inputSchema["properties"]["case_sensitive"][
            "description"
        ]
    )
    assert (
        "line-scoped file selector"
        in search_tool.inputSchema["properties"]["paths"]["description"]
    )
    assert (
        "page through noisy searches"
        in search_tool.inputSchema["properties"]["skip"]["description"]
    )
    assert "matches" in search_output_schema["properties"]
    assert "skipped" in search_output_schema["properties"]
    assert "session_id" in tree_tool.inputSchema["required"]
    assert "session_id" in glob_tool.inputSchema["required"]
    assert (
        "session workdir"
        in tree_tool.inputSchema["properties"]["cwd"]["description"]
    )
    assert (
        "session workdir"
        in glob_tool.inputSchema["properties"]["cwd"]["description"]
    )
    assert "entries" in tree_output_schema["properties"]


@pytest.mark.asyncio
async def test_misc_tool_output_schemas_are_exposed(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    tools = {tool.name: tool for tool in await build_mcp().list_tools()}
    todo_tool = tools["write_todos"]
    secret_tool = tools["secret_scan"]

    todo_output_schema = _output_schema(todo_tool)
    secret_output_schema = _output_schema(secret_tool)
    assert todo_output_schema["title"] == "WriteTodosOutput"
    assert secret_output_schema["title"] == "SecretScanOutput"
    assert "todos" in todo_tool.inputSchema["properties"]
    assert "findings" in secret_output_schema["properties"]


@pytest.mark.asyncio
async def test_job_tool_schema_exposes_durable_companion_contract(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    tools = {tool.name: tool for tool in await build_mcp().list_tools()}
    companion = tools["job"]
    description = (companion.description or "").lower()
    assert "bash" in description
    assert "job_id" in description
    assert "restart" in description
    assert any(
        word in description for word in ("durable", "retained", "persist")
    )

    lines_schema = companion.inputSchema["properties"]["lines"]
    assert lines_schema["minimum"] == 1
    assert lines_schema["maximum"] == 5000
    assert "session_id" in companion.inputSchema["required"]
    assert "cancel" in companion.inputSchema["properties"]

    output_schema = _output_schema(companion)
    assert output_schema["title"] == "JobOutput"
    assert output_schema["$defs"]["JobStatus"]["enum"] == [
        "starting",
        "running",
        "stopping",
        "retrying",
        "succeeded",
        "failed",
        "exited",
        "stopped",
        "lost",
        "unknown",
    ]
    job_info_schema = output_schema["$defs"]["JobInfo"]
    assert "backend" not in job_info_schema["properties"]
    assert "session_id" in job_info_schema["properties"]
    assert "operation" in output_schema["properties"]


@pytest.mark.asyncio
async def test_tool_descriptions_include_runtime_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_MAX_OUTPUT_BYTES", "12345")
    monkeypatch.setenv("WORKGATE_MAX_GREP_RESULTS", "678")
    clear_settings_cache()

    tools = {tool.name: tool for tool in await build_mcp().list_tools()}

    bash_description = tools["bash"].description or ""
    search_description = tools["search"].description or ""
    settings = get_settings()
    assert str(settings.run_shell_default_timeout_s) in bash_description
    assert str(settings.run_shell_max_timeout_s) in bash_description
    assert str(settings.max_grep_results) in search_description


def test_transport_security_uses_exact_base_url_host(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://example.com:8443")
    clear_settings_cache()

    transport_security = transport_security_settings()

    assert "example.com:8443" in transport_security.allowed_hosts
    assert "example.com" not in transport_security.allowed_hosts
    assert "example.com:*" not in transport_security.allowed_hosts
    assert "https://example.com:8443" in transport_security.allowed_origins
    assert "https://example.com" not in transport_security.allowed_origins


def test_transport_security_handles_default_ports_and_ipv6(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://[2001:db8::1]:443")
    clear_settings_cache()

    transport_security = transport_security_settings()

    assert "[2001:db8::1]" in transport_security.allowed_hosts
    assert "[2001:db8::1]:443" in transport_security.allowed_hosts
    assert "2001:db8::1:*" not in transport_security.allowed_hosts
    assert "[2001:db8::1]:*" not in transport_security.allowed_hosts
    assert "https://[2001:db8::1]" in transport_security.allowed_origins


def _assert_tool_annotations(
    tool,
    *,
    read_only: bool,
    destructive: bool,
    idempotent: bool,
    open_world: bool,
):
    annotations = tool.annotations
    assert annotations is not None, tool.name
    assert annotations.readOnlyHint is read_only, tool.name
    assert annotations.destructiveHint is destructive, tool.name
    assert annotations.idempotentHint is idempotent, tool.name
    assert annotations.openWorldHint is open_world, tool.name


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_full_control", [False, True])
async def test_tool_safety_annotations_are_mode_independent(
    tmp_path,
    monkeypatch,
    allow_full_control,
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "WORKGATE_ALLOW_FULL_CONTROL", str(allow_full_control).lower()
    )
    clear_settings_cache()

    tools = {tool.name: tool for tool in await build_mcp().list_tools()}

    assert all(tool.annotations is not None for tool in tools.values())
    _assert_tool_annotations(
        tools["workspace_search"],
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    )
    _assert_tool_annotations(
        tools["bash"],
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=True,
    )
    _assert_tool_annotations(
        tools["write_file"],
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=False,
    )
    _assert_tool_annotations(
        tools["create_file_link"],
        read_only=False,
        destructive=False,
        idempotent=False,
        open_world=True,
    )
    _assert_tool_annotations(
        tools["resize_persistent_shell"],
        read_only=False,
        destructive=False,
        idempotent=False,
        open_world=True,
    )
    _assert_tool_annotations(
        tools["session_start"],
        read_only=False,
        destructive=False,
        idempotent=False,
        open_world=True,
    )
    assert tools["bash"].meta == {
        "securitySchemes": [
            {
                "type": "oauth2",
                "scopes": ["shell:read", "shell:execute"],
            }
        ]
    }


@pytest.mark.asyncio
async def test_read_only_tools_are_annotated(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    tools = {tool.name: tool for tool in await build_mcp().list_tools()}

    read_only_tool_names = {
        "audit_tail",
        "fetch",
        "glob_search",
        "list_file_links",
        "list_files",
        "list_persistent_shells",
        "read",
        "read_persistent_shell_output",
        "read_todos",
        "search",
        "secret_scan",
        "tree_view",
        "version",
        "view_image",
        "workspace_search",
    }
    for name in read_only_tool_names:
        _assert_tool_annotations(
            tools[name],
            read_only=True,
            destructive=False,
            idempotent=True,
            open_world=False,
        )

    for name in set(tools) - read_only_tool_names:
        annotations = tools[name].annotations
        assert annotations is not None, name
        assert annotations.readOnlyHint is False, name


@pytest.mark.asyncio
async def test_agent_bridge_annotations_remain_conservative(
    tmp_path, monkeypatch
):
    config_dir = app_paths().agent_config_dir
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mcpServers": {
                    "docs": {"type": "http", "url": "https://docs.example/mcp"}
                },
            }
        ),
        encoding="utf-8",
    )

    class FakeMcpClientManager:
        async def list_tools(self, name, server):
            return [
                AgentMcpTool(
                    name="search",
                    description="Search docs",
                    input_schema={"type": "object"},
                )
            ]

        async def call_tool(self, name, server, tool, args):
            return {"ok": True}

    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(config_dir.parent))
    monkeypatch.setattr(
        tools_module,
        "AgentMcpClientManager",
        lambda _timeout: FakeMcpClientManager(),
    )
    clear_settings_cache()

    tools = {tool.name: tool for tool in await build_mcp().list_tools()}

    for name in {
        "activate_agent_skill",
        "agent_config_status",
        "list_agent_mcp_servers",
        "list_agent_skills",
    }:
        _assert_tool_annotations(
            tools[name],
            read_only=True,
            destructive=False,
            idempotent=True,
            open_world=False,
        )
    _assert_tool_annotations(
        tools["list_agent_mcp_tools"],
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
    for name in {"call_agent_mcp_tool", "agent_mcp__docs__search"}:
        _assert_tool_annotations(
            tools[name],
            read_only=False,
            destructive=True,
            idempotent=False,
            open_world=True,
        )


def test_oauth_registration_requires_redirect_uri(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://workgate.example.com")
    clear_settings_cache()

    client = TestClient(_add_public_routes_to_mcp_http_app(Starlette())[0])
    response = client.post(
        "/oauth/register", json={"client_name": "Missing Redirects"}
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "invalid_request",
        "error_description": "redirect_uris must be a non-empty list",
    }


def test_oauth_registration_rejects_unsafe_redirect_uris(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://workgate.example.com")
    clear_settings_cache()

    client = TestClient(_add_public_routes_to_mcp_http_app(Starlette())[0])
    for redirect_uri in (
        "javascript:alert(1)",
        "data:text/html,unsafe",
        "http://attacker.example/callback",
        "http://127.0.0.1:9876/callback#fragment",
        "https://client.example/callback#fragment",
        "com.example.app:/oauth2redirect#fragment",
        "ftp://attacker.example/callback",
    ):
        response = client.post(
            "/oauth/register", json={"redirect_uris": [redirect_uri]}
        )
        assert response.status_code == 400
        assert (
            "redirect_uris must be https"
            in response.json()["error_description"]
        )

    loopback = client.post(
        "/oauth/register",
        json={"redirect_uris": ["http://127.0.0.1:9876/callback"]},
    )
    assert loopback.status_code == 201

    private_use = client.post(
        "/oauth/register",
        json={"redirect_uris": ["com.example.app:/oauth2redirect"]},
    )
    assert private_use.status_code == 201


def test_oauth_registration_enforces_size_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://workgate.example.com")
    monkeypatch.setenv("WORKGATE_OAUTH_REGISTRATION_MAX_BODY_BYTES", "1000")
    monkeypatch.setenv("WORKGATE_OAUTH_REGISTRATION_MAX_REDIRECT_URIS", "1")
    monkeypatch.setenv(
        "WORKGATE_OAUTH_REGISTRATION_MAX_REDIRECT_URI_CHARS", "40"
    )
    monkeypatch.setenv("WORKGATE_OAUTH_REGISTRATION_MAX_CLIENT_NAME_CHARS", "8")
    clear_settings_cache()
    oauth_state().clients.clear()

    client = TestClient(_add_public_routes_to_mcp_http_app(Starlette())[0])

    too_many_redirects = client.post(
        "/oauth/register",
        json={
            "redirect_uris": [
                "https://client.example/callback-1",
                "https://client.example/callback-2",
            ]
        },
    )
    assert too_many_redirects.status_code == 400
    assert "at most 1 entries" in too_many_redirects.json()["error_description"]

    long_redirect = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://client.example/" + "x" * 80]},
    )
    assert long_redirect.status_code == 400
    assert "at most 40 characters" in long_redirect.json()["error_description"]

    long_client_name = client.post(
        "/oauth/register",
        json={
            "redirect_uris": ["https://client.example/callback"],
            "client_name": "client-name-is-too-long",
        },
    )
    assert long_client_name.status_code == 400
    assert (
        "client_name must be at most 8"
        in long_client_name.json()["error_description"]
    )

    too_large_body = client.post(
        "/oauth/register",
        content=json.dumps(
            {
                "redirect_uris": ["https://client.example/callback"],
                "client_name": "x" * 2000,
            }
        ),
        headers={"content-type": "application/json"},
    )
    assert too_large_body.status_code == 400
    assert "at most 1000 bytes" in too_large_body.json()["error_description"]


def test_oauth_approved_clients_persist_across_app_rebuild(
    tmp_path, monkeypatch
):
    state_dir = tmp_path / ".state"
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://workgate.example.com")
    monkeypatch.setenv("WORKGATE_OAUTH_ADMIN_PIN", "1234")
    clear_settings_cache()
    oauth_state().clients.clear()

    first_app = TestClient(_add_public_routes_to_mcp_http_app(Starlette())[0])
    registration = first_app.post(
        "/oauth/register",
        json={
            "redirect_uris": ["https://client.example/callback"],
            "client_name": "Persistent client",
        },
    )
    assert registration.status_code == 201
    client_id = registration.json()["client_id"]
    assert not client_store_path().exists()

    verifier = "p" * 64
    approval = first_app.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://client.example/callback",
            "resource": "https://workgate.example.com/mcp",
            "code_challenge": _s256_challenge(verifier),
            "code_challenge_method": "S256",
            "pin": "1234",
        },
        follow_redirects=False,
    )
    assert approval.status_code == 302

    store_path = client_store_path()
    assert store_path.exists()
    if os.name != "nt":
        assert store_path.stat().st_mode & 0o777 == 0o600
    stored = json.loads(store_path.read_text())
    assert [client["client_id"] for client in stored["clients"]] == [client_id]
    assert stored["clients"][0]["approved_at"] is not None
    approved_at = oauth_state().clients[client_id].approved_at

    reloaded_state = OAuthState(state_dir)
    assert reloaded_state.start() == 1

    assert reloaded_state.clients[client_id].redirect_uris == [
        "https://client.example/callback"
    ]
    assert reloaded_state.clients[client_id].client_name == "Persistent client"
    assert reloaded_state.clients[client_id].approved_at == approved_at


def test_oauth_client_approval_rolls_back_when_persistence_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    oauth_state().clients.clear()
    oauth_state().clients["pending"] = OAuthClient(
        client_id="pending",
        redirect_uris=["https://client.example/callback"],
        created_at=10,
    )

    def fail_persistence(_clients) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(
        oauth_service, "persist_approved_clients", fail_persistence
    )

    with pytest.raises(OSError, match="disk unavailable"):
        oauth_service._approve_client("pending", now=20)

    assert oauth_state().clients["pending"].approved_at is None
    assert not client_store_path().exists()


def test_oauth_invalid_client_store_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    store_path = client_store_path()
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(
        RuntimeError, match="Unable to read OAuth client registry"
    ):
        OAuthState(tmp_path / ".state").start()


def test_oauth_registration_caps_dynamic_clients(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://workgate.example.com")
    monkeypatch.setenv("WORKGATE_OAUTH_MAX_DYNAMIC_CLIENTS", "1")
    clear_settings_cache()
    oauth_state().clients.clear()

    client = TestClient(_add_public_routes_to_mcp_http_app(Starlette())[0])
    first = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://client.example/callback-1"]},
    )
    assert first.status_code == 201

    blocked = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://client.example/callback-2"]},
    )
    assert blocked.status_code == 400
    assert blocked.json() == {
        "error": "invalid_request",
        "error_description": "Too many pending OAuth client registrations",
    }

    first_client_id = first.json()["client_id"]
    oauth_state().clients[first_client_id].approved_at = int(time.time())
    second = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://client.example/callback-2"]},
    )
    assert second.status_code == 201


def test_prunes_stale_oauth_clients(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_OAUTH_CLIENT_TTL_S", "10")
    clear_settings_cache()
    oauth_state().clients.clear()
    oauth_state().clients["active"] = OAuthClient(
        client_id="active",
        redirect_uris=["https://client.example/active"],
        created_at=100,
    )
    oauth_state().clients["old"] = OAuthClient(
        client_id="old",
        redirect_uris=["https://client.example/old"],
        created_at=80,
    )
    oauth_state().clients["approved-old"] = OAuthClient(
        client_id="approved-old",
        redirect_uris=["https://client.example/approved"],
        created_at=0,
        approved_at=1,
    )

    _prune_clients(now=100)

    assert set(oauth_state().clients) == {"active", "approved-old"}


def test_oauth_registration_allows_new_client_after_ttl_prune(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://workgate.example.com")
    monkeypatch.setenv("WORKGATE_OAUTH_MAX_DYNAMIC_CLIENTS", "1")
    monkeypatch.setenv("WORKGATE_OAUTH_CLIENT_TTL_S", "1")
    clear_settings_cache()
    oauth_state().clients.clear()
    oauth_state().clients["old"] = OAuthClient(
        client_id="old",
        redirect_uris=["https://client.example/old"],
        created_at=0,
    )

    client = TestClient(_add_public_routes_to_mcp_http_app(Starlette())[0])
    response = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://client.example/callback"]},
    )

    assert response.status_code == 201
    assert "old" not in oauth_state().clients


def test_oauth_authorize_requires_registered_client_and_redirect(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://workgate.example.com")
    monkeypatch.setenv("WORKGATE_OAUTH_ADMIN_PIN", "1234")
    clear_settings_cache()

    client = TestClient(_add_public_routes_to_mcp_http_app(Starlette())[0])
    unknown_response = client.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": "unknown-client",
            "redirect_uri": "https://client.example/callback",
            "resource": "https://workgate.example.com/mcp",
            "pin": "1234",
        },
        follow_redirects=False,
    )
    assert unknown_response.status_code == 200
    assert "Unknown client_id" in unknown_response.text

    register_response = client.post(
        "/oauth/register",
        json={
            "client_name": "Redirect Bound Client",
            "redirect_uris": ["https://client.example/callback"],
        },
    )
    client_id = register_response.json()["client_id"]

    mismatch_response = client.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://attacker.example/callback",
            "resource": "https://workgate.example.com/mcp",
            "pin": "1234",
        },
        follow_redirects=False,
    )

    assert mismatch_response.status_code == 200
    assert (
        "redirect_uri is not registered for this client"
        in mismatch_response.text
    )


def test_oauth_authorize_requires_pkce_and_supported_scope(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://workgate.example.com")
    monkeypatch.setenv("WORKGATE_OAUTH_ADMIN_PIN", "1234")
    clear_settings_cache()

    client = TestClient(_add_public_routes_to_mcp_http_app(Starlette())[0])
    register = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://client.example/callback"]},
    ).json()
    base_data = {
        "response_type": "code",
        "client_id": register["client_id"],
        "redirect_uri": "https://client.example/callback",
        "resource": "https://workgate.example.com/mcp",
        "pin": "1234",
    }

    missing_pkce = client.post(
        "/oauth/authorize", data=base_data, follow_redirects=False
    )
    assert missing_pkce.status_code == 200
    assert "Missing code_challenge" in missing_pkce.text

    unsupported_scope = client.post(
        "/oauth/authorize",
        data={
            **base_data,
            "scope": "shell:read unknown:scope",
            "code_challenge": _s256_challenge("s" * 64),
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert unsupported_scope.status_code == 200
    assert "Unsupported scope: unknown:scope" in unsupported_scope.text


def test_pin_needed_for_oauth_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://workgate.example.com")
    monkeypatch.delenv("WORKGATE_OAUTH_ADMIN_PIN", raising=False)
    clear_settings_cache()
    oauth_state().clients.clear()
    oauth_state().codes.clear()

    client = TestClient(_add_public_routes_to_mcp_http_app(Starlette())[0])
    register = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://client.example/callback"]},
    ).json()
    response = client.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": register["client_id"],
            "redirect_uri": "https://client.example/callback",
            "resource": "https://workgate.example.com/mcp",
            "code_challenge": _s256_challenge("p" * 64),
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert (
        "Admin PIN is required before OAuth approval can continue"
        in response.text
    )
    assert "code=" not in response.text
    assert oauth_state().codes == {}


def test_oauth_scope_enforced_for_rest_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "oauth")
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://workgate.example.com")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    client = TestClient(build_http_app())
    read_token = issue_access_token(
        client_id="limited-client",
        scope="shell:read",
        resource="https://workgate.example.com/mcp",
    )
    headers = {"Authorization": f"Bearer {read_token}"}

    search_response = client.post(
        "/tools/workspace_search", json={"query": "anything"}, headers=headers
    )
    assert search_response.status_code == 200

    bash_response = client.post(
        "/tools/bash",
        json={"session_id": "ABCDEFGH", "command": "echo ok"},
        headers=headers,
    )
    assert bash_response.status_code == 403
    assert (
        "Missing required OAuth scope: shell:execute"
        in bash_response.json()["message"]
    )


def test_oauth_dynamic_registration_authorize_token_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://workgate.example.com")
    monkeypatch.setenv("WORKGATE_OAUTH_ADMIN_PIN", "1234")
    monkeypatch.delenv("WORKGATE_OAUTH_ISSUER", raising=False)
    monkeypatch.delenv("WORKGATE_OAUTH_RESOURCE", raising=False)
    clear_settings_cache()

    client = TestClient(_add_public_routes_to_mcp_http_app(Starlette())[0])
    register_response = client.post(
        "/oauth/register",
        json={
            "client_name": "Regression Client",
            "redirect_uris": ["https://client.example/callback"],
        },
    )

    assert register_response.status_code == 201
    assert register_response.headers["cache-control"] == "no-store"
    registration = register_response.json()
    assert registration["client_id"].startswith("workgate-")
    assert registration["client_name"] == "Regression Client"
    assert registration["redirect_uris"] == ["https://client.example/callback"]

    verifier = "v" * 64
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    authorize_response = client.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": registration["client_id"],
            "redirect_uri": "https://client.example/callback",
            "resource": "https://workgate.example.com/mcp",
            "scope": "shell:read shell:execute",
            "state": "opaque-state",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "pin": "1234",
        },
        follow_redirects=False,
    )

    assert authorize_response.status_code == 302
    redirect = urlparse(authorize_response.headers["location"])
    assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == (
        "https://client.example/callback"
    )
    redirect_query = parse_qs(redirect.query)
    assert redirect_query["iss"] == ["https://workgate.example.com"]
    assert redirect_query["state"] == ["opaque-state"]
    code = redirect_query["code"][0]

    token_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": registration["client_id"],
            "redirect_uri": "https://client.example/callback",
            "resource": "https://workgate.example.com/mcp",
            "code_verifier": verifier,
        },
    )

    assert token_response.status_code == 200
    assert token_response.headers["cache-control"] == "no-store"
    token_payload = token_response.json()
    assert token_payload["token_type"] == "Bearer"
    assert token_payload["scope"] == "shell:read shell:execute"
    assert token_payload["expires_in"] > 0

    claims = validate_bearer_token(token_payload["access_token"])
    assert claims["iss"] == "https://workgate.example.com"
    assert claims["aud"] == "https://workgate.example.com/mcp"
    assert claims["client_id"] == registration["client_id"]
    assert claims["scope"] == "shell:read shell:execute"

    reuse_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": registration["client_id"],
            "redirect_uri": "https://client.example/callback",
            "resource": "https://workgate.example.com/mcp",
            "code_verifier": verifier,
        },
    )

    assert reuse_response.status_code == 400
    assert reuse_response.json() == {
        "error": "invalid_grant",
        "error_description": "Unknown or used code",
    }


def test_oauth_authorize_redirect_preserves_existing_query():
    response = oauth_redirect(
        "https://client.example/callback?existing=value",
        {"code": "abc", "state": "xyz"},
    )
    location = response.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://client.example/callback"
    )
    assert query == {"existing": ["value"], "code": ["abc"], "state": ["xyz"]}


def test_prunes_stale_codes(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_OAUTH_CODE_TTL_S", "10")
    clear_settings_cache()
    oauth_state().codes.clear()

    oauth_state().codes["active"] = AuthCode(
        code="active",
        client_id="client",
        redirect_uri="https://client.example/callback",
        scope="shell:read",
        resource="https://workgate.example.com/mcp",
        code_challenge=None,
        code_challenge_method=None,
        created_at=100,
    )

    k = "old_done"
    oauth_state().codes[k] = AuthCode(
        code=k,
        client_id="client",
        redirect_uri="https://client.example/callback",
        scope="shell:read",
        resource="https://workgate.example.com/mcp",
        code_challenge=None,
        code_challenge_method=None,
        created_at=100,
    )
    setattr(oauth_state().codes[k], "u" + "sed", True)

    oauth_state().codes["old"] = AuthCode(
        code="old",
        client_id="client",
        redirect_uri="https://client.example/callback",
        scope="shell:read",
        resource="https://workgate.example.com/mcp",
        code_challenge=None,
        code_challenge_method=None,
        created_at=80,
    )

    _prune_codes(now=100)
    assert set(oauth_state().codes) == {"active"}


def test_oauth_access_tokens_expire_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("WORKGATE_BASE_URL", raising=False)
    monkeypatch.delenv("WORKGATE_OAUTH_ISSUER", raising=False)
    monkeypatch.delenv("WORKGATE_OAUTH_RESOURCE", raising=False)
    clear_settings_cache()

    token = issue_access_token(
        client_id="test-client",
        scope="shell:execute",
        resource="http://127.0.0.1:8765/mcp",
    )
    claims = validate_bearer_token(token)

    assert claims["exp"] > int(time.time())
    assert claims["client_id"] == "test-client"


def test_oauth_authorize_form_is_mobile_friendly(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    response = _authorize_form(
        {
            "client_id": "client",
            "redirect_uri": "https://example.test/callback",
            "resource": "https://resource.test/mcp",
            "scope": "shell:read",
        }
    )
    body = bytes(response.body).decode("utf-8")

    assert (
        'name="viewport" content="width=device-width, initial-scale=1"' in body
    )
    assert "Only approve this request if you initiated this connection." in body
    assert "Redirect URI:" in body
    assert "example.test/callback" in body
    assert "Unknown client" in body
    assert 'autocomplete="one-time-code"' in body
    assert "overflow-wrap: anywhere" in body


def test_oauth_authorize_form_escapes_reflected_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    marker = chr(60) + "unsafe" + chr(62)
    response = _authorize_form(
        {
            "client_id": "client",
            "redirect_uri": f"https://example.test/cb?x={marker}",
            "resource": f"https://resource.test/{marker}",
            "scope": f"shell:read {marker}",
        },
        error=f"bad {marker}",
    )
    body = bytes(response.body).decode("utf-8")

    assert marker not in body
    assert "&lt;unsafe&gt;" in body
