import json
from typing import cast

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

import workgate.control.http.tool_routes as http_tool_routes_module
from tests.helpers import mcp_text
from workgate import __version__
from workgate.config.settings import clear_settings_cache
from workgate.control.http.app import build_http_app
from workgate.control.mcp.app import build_mcp
from workgate.remote.tool_specs import (
    REMOTE_WORKER_TOOL_NAMES,
    REMOTE_WORKER_TOOL_SPECS,
)
from workgate.remote_worker.worker import WORKER_TOOL_NAMES
from workgate.tools.catalog import ToolCatalog, build_tool_catalog
from workgate.tools.contracts import (
    HttpMethod,
    HttpToolRoute,
    ToolRegistry,
)
from workgate.tools.declarative import (
    DeclarativeToolRegistry,
    _normalize_description,
)
from workgate.tools.local_handlers import (
    UnknownLocalToolError,
    call_local_tool,
    local_tool_handlers,
)

LOCAL_MCP_TOOL_NAMES = {
    "audit_tail",
    "bash",
    "read",
    "search",
    "workspace_search",
    "fetch",
    "session_start",
    "session_change_cwd",
    "session_end",
    "session_copy",
    "version",
    "run_python_code",
    "send_persistent_shell_input",
    "resize_persistent_shell",
    "read_persistent_shell_output",
    "kill_persistent_shell",
    "list_persistent_shells",
    "list_files",
    "tree_view",
    "glob_search",
    "write_file",
    "edit_lines",
    "hashline_edit",
    "apply_patch",
    "delete_file_or_dir",
    "create_file_link",
    "list_file_links",
    "revoke_file_link",
    "secret_scan",
    "read_todos",
    "write_todos",
    "job",
    "view_image",
}


def test_normalize_description_cleans_docstring_text():
    assert _normalize_description(
        """
        First line with   extra   spaces.
          Continued line.

            Second paragraph
            with tabs	and spaces.

        """
    ) == (
        "First line with extra spaces. Continued line.\n\n"
        "Second paragraph with tabs and spaces."
    )


REMOTE_MCP_TOOL_NAMES = {
    "remote_admin",
}


@pytest.mark.asyncio
async def test_mcp_local_and_remote_tool_surface_is_stable(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_MODE", "mcp")
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    names = {tool.name for tool in await build_mcp().list_tools()}

    assert names == LOCAL_MCP_TOOL_NAMES | REMOTE_MCP_TOOL_NAMES


@pytest.mark.asyncio
async def test_stdio_mcp_hides_http_server_backed_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_MODE", "stdio")
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    names = {tool.name for tool in await build_mcp().list_tools()}

    assert names == LOCAL_MCP_TOOL_NAMES - {
        "create_file_link",
        "list_file_links",
        "revoke_file_link",
    }
    assert names.isdisjoint(REMOTE_MCP_TOOL_NAMES)


@pytest.mark.asyncio
async def test_model_facing_tools_require_session_id_by_default(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    tools = {tool.name: tool for tool in await build_mcp().list_tools()}
    sessionless_allowlist = {
        "session_start",
        "version",
        "workspace_search",
        "fetch",
        "send_persistent_shell_input",
        "resize_persistent_shell",
        "read_persistent_shell_output",
        "kill_persistent_shell",
        "list_persistent_shells",
        "remote_admin",
    }

    assert sessionless_allowlist <= set(tools)

    unexpected_sessionless = set()
    for name, tool in tools.items():
        required = set(tool.inputSchema.get("required", []))
        has_single_session = "session_id" in required
        has_copy_sessions = {"src_session_id", "dst_session_id"} <= required
        if (
            not (has_single_session or has_copy_sessions)
            and name not in sessionless_allowlist
        ):
            unexpected_sessionless.add(name)

    assert unexpected_sessionless == set()
    for name in sessionless_allowlist:
        assert "session_id" not in set(
            tools[name].inputSchema.get("required", [])
        )


@pytest.mark.asyncio
async def test_hashline_edit_is_model_facing_default(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    mcp = build_mcp()
    assert mcp.instructions is not None
    for concept in (
        "read",
        "search",
        "hashline_edit",
        "snapshot_id",
        "line:text",
        "edit_lines",
    ):
        assert concept in mcp.instructions

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    descriptions = {
        name: tools[name].description or ""
        for name in (
            "read",
            "search",
            "write_file",
            "edit_lines",
            "hashline_edit",
        )
    }

    for concept in ("snapshot_id", "line:text", "hashline_edit"):
        assert concept in descriptions["read"]
    for concept in ("grounding", "hashline_edit", "gitignore"):
        assert concept in descriptions["search"]
    assert "hashline_edit" in descriptions["write_file"]
    for concept in ("snapshot_id", "hashline_edit"):
        assert concept in descriptions["edit_lines"]
    for concept in ("read", "search", "snapshot", "SWAP", "INSERT"):
        assert concept in descriptions["hashline_edit"]


def test_remote_registry_declares_only_remote_admin(monkeypatch):
    monkeypatch.setenv("WORKGATE_MODE", "mcp")
    monkeypatch.setenv("WORKGATE_REMOTE_ENABLED", "true")
    clear_settings_cache()

    registry = cast(
        DeclarativeToolRegistry,
        next(
            registry
            for registry in build_tool_catalog().registries
            if registry.name == "remote"
        ),
    )
    names = {tool.name for tool in registry.tools}
    route_names = {route.tool_name for route in registry.http_routes()}
    handler_names = set(registry.http_handlers())
    legacy_names = {
        "remote_invite",
        "remote_list_machines",
        "remote_revoke_machine",
        "remote_rename_machine",
        "remote_copy_file",
        "remote_copy_dir",
        "remote_pull_file",
        "remote_push_file",
        "remote_pull_dir",
        "remote_push_dir",
    }

    assert names == {"remote_admin"}
    assert "remote_admin" in route_names
    assert "remote_admin" in handler_names
    assert "remote" not in route_names
    assert "remote" not in handler_names
    assert names.isdisjoint(legacy_names)
    assert route_names.isdisjoint(legacy_names)
    assert handler_names.isdisjoint(legacy_names)


def test_remote_worker_specs_drive_http_and_worker_allowlist(monkeypatch):
    monkeypatch.setenv("WORKGATE_MODE", "mcp")
    monkeypatch.setenv("WORKGATE_REMOTE_ENABLED", "true")
    clear_settings_cache()

    exposed_specs = [
        spec for spec in REMOTE_WORKER_TOOL_SPECS if spec.expose_http
    ]
    spec_names = {spec.public_name for spec in exposed_specs}
    worker_tools = {spec.worker_tool for spec in REMOTE_WORKER_TOOL_SPECS}
    route_by_name = {
        route.tool_name: route
        for registry in build_tool_catalog().registries
        for route in registry.http_routes()
    }
    handler_names = set(local_tool_handlers())

    assert len(spec_names) == len(exposed_specs)
    assert worker_tools == REMOTE_WORKER_TOOL_NAMES
    assert WORKER_TOOL_NAMES == REMOTE_WORKER_TOOL_NAMES
    assert spec_names <= set(route_by_name)
    assert spec_names <= handler_names
    for spec in exposed_specs:
        route = route_by_name[spec.public_name]
        assert route.method == "POST"
        assert route.path == spec.http_path


def test_http_openapi_version_matches_package_version(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    response = TestClient(build_http_app()).get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["version"] == __version__


def test_http_public_version_endpoint_reports_package_version(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    response = TestClient(build_http_app()).get("/version")

    assert response.status_code == 200
    assert response.json()["version"] == __version__
    assert response.json()["python"]


@pytest.mark.asyncio
async def test_http_version_matches_mcp_tool_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    http_payload = TestClient(build_http_app()).get("/tools/version").json()
    mcp_response = await build_mcp().call_tool("version", {})

    assert http_payload == _mcp_payload_data(mcp_response)
    assert http_payload["version"] == __version__


def _mcp_payload_data(response):
    return (
        response[1]
        if isinstance(response, tuple)
        else json.loads(mcp_text(response))
    )


@pytest.mark.asyncio
async def test_http_list_files_matches_mcp_tool_payload(tmp_path, monkeypatch):
    (tmp_path / "alpha.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    client = TestClient(build_http_app())
    session = client.post("/tools/session_start", json={"workdir": "."}).json()
    args = {"session_id": session["session_id"], "path": "."}
    http_payload = client.post("/tools/list_files", json=args).json()
    mcp_response = await build_mcp().call_tool("list_files", args)
    assert http_payload == _mcp_payload_data(mcp_response)


@pytest.mark.asyncio
async def test_http_read_todos_matches_mcp_tool_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    client = TestClient(build_http_app())
    session = client.post("/tools/session_start", json={"workdir": "."}).json()
    args = {"session_id": session["session_id"]}
    http_payload = client.get("/tools/todo", params=args).json()
    mcp_response = await build_mcp().call_tool("read_todos", args)

    assert http_payload == _mcp_payload_data(mcp_response)


@pytest.mark.asyncio
async def test_http_secret_scan_matches_mcp_tool_payload(tmp_path, monkeypatch):
    (tmp_path / "safe.txt").write_text("hello\n", encoding="utf-8")
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    client = TestClient(build_http_app())
    session = client.post("/tools/session_start", json={"workdir": "."}).json()
    args = {"session_id": session["session_id"], "cwd": ".", "max_results": 10}
    http_payload = client.post("/tools/secret_scan", json=args).json()
    mcp_response = await build_mcp().call_tool("secret_scan", args)

    assert http_payload == _mcp_payload_data(mcp_response)


def test_http_tool_name_is_not_request_overridable(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    client = TestClient(build_http_app())
    session = client.post("/tools/session_start", json={"workdir": "."}).json()
    response = client.get(
        "/tools/todo",
        params={
            "session_id": session["session_id"],
            "tool_name": "list_persistent_shells",
        },
    )

    assert response.status_code == 200
    assert "todos" in response.json()


def test_get_http_tools_disable_response_caching(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    client = TestClient(build_http_app())
    response = client.get("/tools/version")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["expires"] == "0"


def test_http_tool_missing_required_arg_returns_validation_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    client = TestClient(build_http_app())
    for path, payload in (
        ("/tools/read", {}),
        ("/tools/bash", {"command": "echo ok"}),
        ("/tools/job", {}),
    ):
        response = client.post(path, json=payload)

        assert response.status_code == 400
        assert response.json() == {
            "error": "validation_error",
            "message": "Missing required argument: session_id",
        }


def test_http_get_query_params_are_type_coerced(tmp_path, monkeypatch):
    (tmp_path / "artifact.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_MODE", "http")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    from workgate.ops import downloads as download_ops

    clock = {"now": 1_000.0}
    monkeypatch.setattr(download_ops, "now_s", lambda: clock["now"])

    client = TestClient(build_http_app())
    session = client.post("/tools/session_start", json={"workdir": "."}).json()
    create_response = client.post(
        "/tools/file_link/create",
        json={
            "session_id": session["session_id"],
            "path": "artifact.txt",
            "ttl_s": 10,
        },
    )
    assert create_response.status_code == 200

    clock["now"] = 1_020.0
    include_response = client.get(
        "/tools/file_link/list",
        params={
            "session_id": session["session_id"],
            "include_expired": "true",
        },
    )
    assert include_response.status_code == 200
    assert len(include_response.json()["links"]) == 1

    prune_response = client.get(
        "/tools/file_link/list",
        params={
            "session_id": session["session_id"],
            "include_expired": "false",
        },
    )
    assert prune_response.status_code == 200
    assert prune_response.json()["links"] == []


def test_todos_are_session_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    from workgate.ops.todo import read_todos_execute, write_todos_execute
    from workgate.tool_session.store import get_tool_session_store

    store = get_tool_session_store()
    store.clear()
    first = store.create_session(workdir=".").session_id
    second = store.create_session(workdir=".").session_id
    first_items = [{"id": "first", "content": "one"}]
    second_items = [{"id": "second", "content": "two"}]

    write_todos_execute(first_items, first)
    write_todos_execute(second_items, second)

    assert read_todos_execute(first).todos[0].id == "first"
    assert read_todos_execute(first).todos[0].content == "one"
    assert read_todos_execute(second).todos[0].id == "second"
    assert read_todos_execute(second).todos[0].content == "two"


def test_http_tool_file_not_found_returns_json_error(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    client = TestClient(build_http_app())
    session = client.post("/tools/session_start", json={"workdir": "."}).json()
    response = client.post(
        "/tools/read",
        json={"session_id": session["session_id"], "path": "missing.txt"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "FileNotFoundError",
        "message": f"FileNotFoundError: {tmp_path / 'missing.txt'}",
    }


def test_http_tool_unexpected_error_returns_json_error(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    async def broken_call_local_tool(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        http_tool_routes_module, "call_http_tool", broken_call_local_tool
    )

    response = TestClient(build_http_app(), raise_server_exceptions=False).post(
        "/tools/read", json={"path": "a.txt"}
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": "internal_error",
        "message": "Unhandled RuntimeError: boom",
    }


def test_http_mode_hides_remote_worker_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_MODE", "http")
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    response = TestClient(build_http_app()).post(
        "/tools/run_remote_shell_command", json={"command": "echo ok"}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.asyncio
async def test_mcp_tool_missing_required_arg_uses_fastmcp_tool_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    with pytest.raises(ToolError, match="validation errors for readArguments"):
        await build_mcp().call_tool("read", {})


@pytest.mark.asyncio
async def test_mcp_remote_facade_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    tools = {tool.name for tool in await build_mcp().list_tools()}
    assert "remote" not in tools

    with pytest.raises(ToolError, match="Unknown tool: remote"):
        await build_mcp().call_tool(
            "remote",
            {
                "machine": "worker-a",
                "op": "bash",
                "args": {"command": "echo ok"},
            },
        )


@pytest.mark.asyncio
async def test_mcp_unknown_tool_uses_fastmcp_tool_error(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    with pytest.raises(ToolError, match="Unknown tool: no_such_tool"):
        await build_mcp().call_tool("no_such_tool", {})


def test_http_tool_routes_reject_unsupported_methods():
    class RegistryWithUnsupportedRoute(ToolRegistry):
        def http_routes(self):
            return [
                HttpToolRoute(
                    cast(HttpMethod, "PUT"), "/tools/example", "read_todos"
                )
            ]

    catalog = ToolCatalog((RegistryWithUnsupportedRoute(),))

    with pytest.raises(ValueError, match="Unsupported HTTP tool method 'PUT'"):
        build_http_app(tool_catalog=catalog)


@pytest.mark.asyncio
async def test_local_handlers_report_unknown_tool():
    class EmptyRegistry(ToolRegistry):
        pass

    catalog = ToolCatalog((EmptyRegistry(),))

    with pytest.raises(
        UnknownLocalToolError, match="Unknown local tool: example_tool"
    ):
        await call_local_tool("example_tool", {}, catalog=catalog)


@pytest.mark.asyncio
async def test_local_handlers_are_collected_from_explicit_catalog():
    async def example_handler(args):
        return {"from_registry": args["value"]}

    class ExampleRegistry(ToolRegistry):
        def http_handlers(self):
            return {"example_tool": example_handler}

    catalog = ToolCatalog((ExampleRegistry(),))

    assert await call_local_tool(
        "example_tool", {"value": 42}, catalog=catalog
    ) == {"from_registry": 42}


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_bridge_enabled", ["false", "true"])
async def test_mcp_tools_have_matching_http_routes_and_handlers(
    tmp_path, monkeypatch, agent_bridge_enabled
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / "agents"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", agent_bridge_enabled)
    clear_settings_cache()
    catalog = build_tool_catalog()

    mcp_tool_names = {
        tool.name for tool in await build_mcp(tool_catalog=catalog).list_tools()
    }
    route_tool_names = {route.tool_name for route in catalog.http_routes()}
    handler_tool_names = set(catalog.local_handlers())

    internal_worker_handlers = REMOTE_WORKER_TOOL_NAMES - {
        spec.worker_tool
        for spec in REMOTE_WORKER_TOOL_SPECS
        if spec.expose_http
    }
    if agent_bridge_enabled == "false":
        internal_worker_handlers -= {
            "list_agent_skills",
            "activate_agent_skill",
            "read_agent_skill_file",
        }

    assert route_tool_names == mcp_tool_names - {"view_image"}
    assert handler_tool_names == mcp_tool_names | internal_worker_handlers


@pytest.mark.asyncio
async def test_run_python_code_creates_temp_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    session = await call_local_tool("session_start", {"workdir": "."})
    payload = await call_local_tool(
        "run_python_code",
        {
            "session_id": session.session_id,
            "code": "print('py314')",
            "cwd": ".",
        },
    )

    assert payload.mode == "command"
    assert payload.cwd == str(tmp_path)
    assert payload.result["ok"] is True
    assert payload.result["stdout"].splitlines() == ["py314"]
    assert payload.script_path.endswith(".py")
