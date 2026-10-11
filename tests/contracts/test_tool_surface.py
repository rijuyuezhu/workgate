import json

import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from tests.support.paired import build_paired_http_app
from tests.support.responses import mcp_text
from workgate import __version__
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.http.app import build_http_app
from workgate.control.mcp.app import build_mcp
from workgate.tools.catalog import build_tool_catalog
from workgate.tools.declarative import _normalize_description

LOCAL_MCP_TOOL_NAMES = {
    "audit_tail",
    "bash",
    "browser_act",
    "browser_run_script",
    "browser_session",
    "browser_snapshot",
    "read",
    "search",
    "workspace_search",
    "fetch",
    "session_start",
    "session_change_workdir",
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
    "gui_action",
    "gui_list",
    "gui_state",
    "write_file",
    "edit_lines",
    "executor",
    "hashline_edit",
    "apply_patch",
    "delete_file_or_dir",
    "create_file_link",
    "list_file_links",
    "revoke_file_link",
    "secret_scan",
    "task",
    "task_plan",
    "read_todos",
    "write_todos",
    "job",
    "view_image",
    "workspace_open",
    "workspace_snapshot",
    "workspace_continuation",
    "workspace_task_control",
    "workspace_end",
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


@pytest.mark.asyncio
async def test_mcp_tool_surface_is_stable(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_MODE", "mcp")
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    names = {tool.name for tool in await build_mcp().list_tools()}

    assert names == LOCAL_MCP_TOOL_NAMES
    assert "remote_admin" not in names


@pytest.mark.asyncio
async def test_session_copy_public_defaults_are_safe_and_tracked(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("WORKGATE_MODE", "mcp")
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    tools = {tool.name: tool for tool in await build_mcp().list_tools()}
    properties = tools["session_copy"].input_schema["properties"]
    overwrite = properties["overwrite"]
    background = properties["background"]

    assert overwrite["default"] is False
    assert "set true explicitly" in overwrite["description"]
    assert background["default"] is True
    assert "Set false explicitly" in background["description"]


@pytest.mark.asyncio
async def test_browser_actions_are_structured_and_bounded(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_MODE", "mcp")
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    tools = {tool.name: tool for tool in await build_mcp().list_tools()}
    actions = tools["browser_act"].input_schema["properties"]["actions"]

    assert actions["minItems"] == 1
    assert actions["maxItems"] == 50
    assert actions["items"]["discriminator"]["propertyName"] == "action"
    assert len(actions["items"]["oneOf"]) >= 8


@pytest.mark.asyncio
async def test_stdio_mcp_hides_http_server_backed_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_MODE", "stdio")
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    names = {tool.name for tool in await build_mcp().list_tools()}

    assert names == LOCAL_MCP_TOOL_NAMES - {
        "create_file_link",
        "list_file_links",
        "revoke_file_link",
        "workspace_open",
        "workspace_snapshot",
        "workspace_task_control",
        "workspace_continuation",
        "workspace_end",
        "executor",
    }


@pytest.mark.asyncio
async def test_model_facing_tools_require_explicit_semantic_identity(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    tools = {tool.name: tool for tool in await build_mcp().list_tools()}
    identityless_allowlist = {
        "audit_tail",
        "executor",
        "session_start",
        "task",
        "version",
    }

    assert identityless_allowlist <= set(tools)

    unexpected_identityless = set()
    for name, tool in tools.items():
        required = set(tool.input_schema.get("required", []))
        has_single_session = "session_id" in required
        has_copy_sessions = {"src_session_id", "dst_session_id"} <= required
        has_task = "task_id" in required
        if (
            not (has_single_session or has_copy_sessions or has_task)
            and name not in identityless_allowlist
        ):
            unexpected_identityless.add(name)

    assert unexpected_identityless == set()
    assert "session_id" not in set(
        tools["task"].input_schema.get("required", [])
    )
    assert "task_id" not in set(tools["task"].input_schema.get("required", []))
    for name in ("task", "task_plan", "write_todos"):
        assert "expected_revision" not in set(
            tools[name].input_schema.get("properties", {})
        )
    audit_properties = set(
        tools["audit_tail"].input_schema.get("properties", {})
    )
    assert {"task_id", "session_id"} <= audit_properties
    assert not set(tools["audit_tail"].input_schema.get("required", []))


@pytest.mark.asyncio
async def test_hashline_edit_is_model_facing_default(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    mcp = build_mcp()
    assert mcp.instructions is not None
    for concept in (
        "read",
        "search",
        "hashline_edit",
    ):
        assert concept in mcp.instructions
    assert len(mcp.instructions) < 3000

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
    for name in ("hashline_edit", "edit_lines"):
        assert len(descriptions[name]) < 300
    assert (
        "SWAP"
        in tools["hashline_edit"].input_schema["properties"]["input"][
            "description"
        ]
    )


def test_final_catalog_has_no_legacy_remote_registry_or_routes(monkeypatch):
    monkeypatch.setenv("WORKGATE_MODE", "mcp")
    clear_settings_cache()

    catalog = build_tool_catalog()
    registry_names = {registry.name for registry in catalog.registries}
    assert "remote" not in registry_names


def test_http_openapi_version_matches_package_version(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    response = TestClient(build_http_app()).get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["version"] == __version__


def test_http_public_version_endpoint_reports_package_version(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    response = TestClient(build_http_app()).get("/version")

    assert response.status_code == 200
    assert response.json()["version"] == __version__
    assert response.json()["python"]


@pytest.mark.asyncio
async def test_http_version_matches_mcp_tool_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    http_payload = (
        TestClient(build_http_app())
        .post("/tools/version", json={})
        .json()["structuredContent"]
    )
    mcp_response = await build_mcp().call_tool("version", {})

    assert http_payload == _mcp_payload_data(mcp_response)
    assert http_payload["version"] == __version__


def _mcp_payload_data(response):
    if isinstance(response, CallToolResult):
        assert isinstance(response.structured_content, dict)
        return response.structured_content
    return (
        response[1]
        if isinstance(response, tuple)
        else json.loads(mcp_text(response))
    )


def _build_paired_surface_http_app():
    """Build the public HTTP surface over one final paired executor."""
    app, _harness = build_paired_http_app(get_settings())
    return app


@pytest.mark.asyncio
async def test_http_list_files_matches_mcp_tool_payload(tmp_path, monkeypatch):
    (tmp_path / "alpha.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    app = _build_paired_surface_http_app()
    client = TestClient(app)
    session = client.post("/tools/session_start", json={}).json()[
        "structuredContent"
    ]
    assert session["workdir"] == str(tmp_path)
    args = {"session_id": session["session_id"], "path": "."}
    http_payload = client.post("/tools/list_files", json=args).json()[
        "structuredContent"
    ]
    mcp_response = await build_mcp(runtime=app.state.control_runtime).call_tool(
        "list_files", args
    )
    assert http_payload == _mcp_payload_data(mcp_response)


@pytest.mark.asyncio
async def test_model_facing_tools_use_explicit_mcp_text(tmp_path, monkeypatch):
    target = tmp_path / "context.txt"
    target.write_text("alpha\nneedle\nomega\n", encoding="utf-8")
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    app = _build_paired_surface_http_app()
    mcp = build_mcp(runtime=app.state.control_runtime)

    started = await mcp.call_tool("session_start", {})
    assert isinstance(started, CallToolResult)
    assert isinstance(started.structured_content, dict)
    assert isinstance(started.content[0], TextContent)
    assert started.structured_content["session_id"] in started.content[0].text
    assert started.structured_content["workdir"] in started.content[0].text
    assert started.structured_content["workdir"] == str(tmp_path)
    assert not started.content[0].text.lstrip().startswith("{")
    session_id = started.structured_content["session_id"]

    read = await mcp.call_tool(
        "read", {"session_id": session_id, "path": "context.txt:1-3"}
    )
    assert isinstance(read, CallToolResult)
    assert isinstance(read.structured_content, dict)
    assert isinstance(read.content[0], TextContent)
    assert read.content[0].text == read.structured_content["content"]

    search = await mcp.call_tool(
        "search",
        {
            "session_id": session_id,
            "pattern": "needle",
            "regex": False,
            "paths": "context.txt",
        },
    )
    assert isinstance(search, CallToolResult)
    assert isinstance(search.structured_content, dict)
    assert isinstance(search.content[0], TextContent)
    numbered = search.structured_content["numbered_content"]
    stderr = search.structured_content["stderr"].strip()
    expected_search_text = (
        numbered
        or stderr
        or ("No matches." if search.structured_content["count"] == 0 else "")
    )
    assert search.content[0].text == expected_search_text

    bash = await mcp.call_tool(
        "bash",
        {"session_id": session_id, "command": "printf shell-output"},
    )
    assert isinstance(bash, CallToolResult)
    assert isinstance(bash.structured_content, dict)
    assert isinstance(bash.content[0], TextContent)
    assert bash.content[0].text == "shell-output"

    tree = await mcp.call_tool(
        "tree_view", {"session_id": session_id, "cwd": ".", "depth": 1}
    )
    assert isinstance(tree, CallToolResult)
    assert isinstance(tree.structured_content, dict)
    assert isinstance(tree.content[0], TextContent)
    assert tree.content[0].text == "\n".join(tree.structured_content["entries"])

    glob = await mcp.call_tool(
        "glob_search", {"session_id": session_id, "pattern": "**/*.txt"}
    )
    assert isinstance(glob, CallToolResult)
    assert isinstance(glob.structured_content, dict)
    assert isinstance(glob.content[0], TextContent)
    paths = glob.structured_content["paths"]
    assert glob.content[0].text == (
        "\n".join(paths) if paths else "No matches."
    )


@pytest.mark.asyncio
async def test_edit_tools_return_compact_grounded_mcp_content(
    tmp_path, monkeypatch
):
    target = tmp_path / "edit.txt"
    target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    app = _build_paired_surface_http_app()
    client = TestClient(app)
    session = client.post("/tools/session_start", json={"workdir": "."}).json()[
        "structuredContent"
    ]
    session_id = session["session_id"]
    compact_context_keys = {
        "path",
        "snapshot_id",
        "start_line",
        "end_line",
        "numbered_content",
        "truncated",
    }

    first_read = client.post(
        "/tools/read",
        json={"session_id": session_id, "path": "edit.txt:2"},
    ).json()["structuredContent"]
    http_edit = client.post(
        "/tools/edit_lines",
        json={
            "session_id": session_id,
            "path": "edit.txt",
            "start_line": 2,
            "end_line": 2,
            "replacement": "TWO",
            "snapshot_id": first_read["file"]["snapshot_id"],
        },
    ).json()["structuredContent"]
    assert set(http_edit["context"]) == compact_context_keys
    assert "content" not in http_edit["context"]
    assert "lines" not in http_edit["context"]

    mcp = build_mcp(runtime=app.state.control_runtime)
    second_read = client.post(
        "/tools/read",
        json={"session_id": session_id, "path": "edit.txt:3"},
    ).json()["structuredContent"]
    mcp_edit = await mcp.call_tool(
        "edit_lines",
        {
            "session_id": session_id,
            "path": "edit.txt",
            "start_line": 3,
            "end_line": 3,
            "replacement": "THREE",
            "snapshot_id": second_read["file"]["snapshot_id"],
        },
    )
    assert isinstance(mcp_edit, CallToolResult)
    assert isinstance(mcp_edit.structured_content, dict)
    assert set(mcp_edit.structured_content) == set(http_edit)
    assert set(mcp_edit.structured_content["context"]) == compact_context_keys
    assert len(mcp_edit.content) == 1
    assert isinstance(mcp_edit.content[0], TextContent)
    assert (
        mcp_edit.content[0].text
        == (mcp_edit.structured_content["context"]["numbered_content"])
    )
    assert not mcp_edit.content[0].text.lstrip().startswith("{")

    hash_read = client.post(
        "/tools/read",
        json={"session_id": session_id, "path": "edit.txt:1-4"},
    ).json()["structuredContent"]
    header = hash_read["content"].splitlines()[0]
    hash_edit = await mcp.call_tool(
        "hashline_edit",
        {
            "session_id": session_id,
            "input": (header + "\n1:one\n+ONE\n\n4:four\n+FOUR"),
        },
    )
    assert isinstance(hash_edit, CallToolResult)
    assert isinstance(hash_edit.structured_content, dict)
    assert "context" not in hash_edit.structured_content
    assert hash_edit.structured_content["hunk_count"] == 2
    assert all(
        set(hunk["context"]) == compact_context_keys
        for hunk in hash_edit.structured_content["hunks"]
    )
    assert len(hash_edit.content) == 1
    assert isinstance(hash_edit.content[0], TextContent)
    assert hash_edit.content[0].text == "\n\n".join(
        hunk["context"]["numbered_content"]
        for hunk in hash_edit.structured_content["hunks"]
    )
    assert not hash_edit.content[0].text.lstrip().startswith("{")


@pytest.mark.asyncio
async def test_http_read_todos_matches_mcp_tool_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    app = _build_paired_surface_http_app()
    client = TestClient(app)
    task = client.post("/tools/task", json={"action": "create"}).json()[
        "structuredContent"
    ]
    args = {"task_id": task["result"]["task_id"]}
    http_payload = client.post("/tools/read_todos", json=args).json()[
        "structuredContent"
    ]
    mcp_response = await build_mcp(runtime=app.state.control_runtime).call_tool(
        "read_todos", args
    )

    assert http_payload == _mcp_payload_data(mcp_response)


@pytest.mark.asyncio
async def test_http_task_matches_mcp_and_progress_mutates_it(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    app = _build_paired_surface_http_app()
    client = TestClient(app)
    created = client.post(
        "/tools/task",
        json={"action": "create", "objective": "Durable task"},
    )
    assert created.status_code == 200
    task_id = created.json()["structuredContent"]["result"]["task_id"]
    reported = client.post(
        "/tools/task",
        json={
            "action": "report",
            "task_id": task_id,
            "summary": "Reported over HTTP",
        },
    )
    assert reported.status_code == 200

    args = {"action": "get", "task_id": task_id}
    http_payload = client.post("/tools/task", json=args).json()[
        "structuredContent"
    ]
    mcp_response = await build_mcp(runtime=app.state.control_runtime).call_tool(
        "task", args
    )

    mcp_payload = _mcp_payload_data(mcp_response)
    assert isinstance(mcp_payload, dict)
    assert http_payload == mcp_payload
    assert http_payload["result"]["objective"] == "Durable task"
    assert http_payload["result"]["progress"]["summary"] == "Reported over HTTP"


@pytest.mark.asyncio
async def test_unrouted_todo_registry_stubs_fail_closed():
    from workgate.tools.registry.todo import read_todos, write_todos

    with pytest.raises(RuntimeError, match="requires control routing"):
        await read_todos.func("task_AAAAAAAAAAAAAAAAAAAAAA")
    with pytest.raises(RuntimeError, match="requires control routing"):
        await write_todos.func("task_AAAAAAAAAAAAAAAAAAAAAA", [])


def test_http_task_router_validates_actions_and_covers_lifecycle(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    client = TestClient(_build_paired_surface_http_app())

    invalid_create = client.post(
        "/tools/task",
        json={
            "action": "create",
            "task_id": "task_AAAAAAAAAAAAAAAAAAAAAA",
        },
    )
    invalid_create_report = client.post(
        "/tools/task", json={"action": "create", "summary": "not allowed"}
    )
    missing_task = client.post("/tools/task", json={"action": "get"})
    assert invalid_create.json()["isError"] is True
    assert invalid_create_report.json()["isError"] is True
    assert missing_task.json()["isError"] is True

    task_id = client.post("/tools/task", json={"action": "create"}).json()[
        "structuredContent"
    ]["result"]["task_id"]
    invalid_get = client.post(
        "/tools/task",
        json={"action": "get", "task_id": task_id, "summary": "not allowed"},
    )
    invalid_block = client.post(
        "/tools/task",
        json={
            "action": "block",
            "task_id": task_id,
            "label": "not allowed",
        },
    )
    assert invalid_get.json()["isError"] is True
    assert invalid_block.json()["isError"] is True

    blocked = client.post(
        "/tools/task",
        json={"action": "block", "task_id": task_id},
    )
    resumed = client.post(
        "/tools/task",
        json={"action": "resume", "task_id": task_id},
    )
    finished = client.post(
        "/tools/task",
        json={"action": "finish", "task_id": task_id},
    )
    assert blocked.status_code == 200
    assert resumed.status_code == 200
    assert finished.status_code == 200

    rejected_session = client.post(
        "/tools/session_start", json={"workdir": ".", "task_id": task_id}
    )
    assert rejected_session.json()["isError"] is True
    assert rejected_session.json()["isError"] is True

    deleted = client.post(
        "/tools/task",
        json={"action": "delete", "task_id": task_id},
    )
    assert deleted.status_code == 200
    assert deleted.json()["structuredContent"]["result"]["deleted"] is True


def test_session_start_rejects_terminal_task_attachment(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    client = TestClient(_build_paired_surface_http_app())
    task_id = client.post("/tools/task", json={"action": "create"}).json()[
        "structuredContent"
    ]["result"]["task_id"]
    cancelled = client.post(
        "/tools/task",
        json={
            "action": "cancel",
            "task_id": task_id,
        },
    )
    assert cancelled.status_code == 200

    start = client.post(
        "/tools/session_start",
        json={"workdir": ".", "task_id": task_id},
    )
    assert start.status_code == 200
    assert start.json()["isError"] is True


def test_http_task_plan_replaces_state_and_rejects_typos(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    client = TestClient(
        _build_paired_surface_http_app(), raise_server_exceptions=False
    )
    task_id = client.post("/tools/task", json={"action": "create"}).json()[
        "structuredContent"
    ]["result"]["task_id"]
    first = client.post(
        "/tools/task_plan",
        json={
            "task_id": task_id,
            "steps": [{"id": "one", "content": "first"}],
        },
    )
    replacement = client.post(
        "/tools/task_plan",
        json={
            "task_id": task_id,
            "steps": [{"id": "one", "content": "replacement"}],
        },
    )
    assert first.status_code == 200
    assert replacement.status_code == 200
    assert (
        replacement.json()["structuredContent"]["plan"]["steps"][0]["content"]
        == "replacement"
    )

    typo = client.post(
        "/tools/task_plan",
        json={
            "task_id": task_id,
            "steps": [
                {
                    "id": "one",
                    "content": "must reject typo",
                    "state": "completed",
                }
            ],
        },
    )
    assert typo.json()["isError"] is True
    assert typo.json()["content"][0]["text"] == "Error executing tool task_plan"


@pytest.mark.asyncio
async def test_http_secret_scan_matches_mcp_tool_payload(tmp_path, monkeypatch):
    (tmp_path / "safe.txt").write_text("hello\n", encoding="utf-8")
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    app = _build_paired_surface_http_app()
    client = TestClient(app)
    session = client.post("/tools/session_start", json={"workdir": "."}).json()[
        "structuredContent"
    ]
    args = {"session_id": session["session_id"], "cwd": ".", "max_results": 10}
    http_payload = client.post("/tools/secret_scan", json=args).json()[
        "structuredContent"
    ]
    mcp_response = await build_mcp(runtime=app.state.control_runtime).call_tool(
        "secret_scan", args
    )

    assert http_payload == _mcp_payload_data(mcp_response)


def test_http_tool_name_is_not_request_overridable(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    client = TestClient(_build_paired_surface_http_app())
    task = client.post("/tools/task", json={"action": "create"}).json()[
        "structuredContent"
    ]
    response = client.post(
        "/tools/read_todos",
        json={
            "task_id": task["result"]["task_id"],
            "tool_name": "list_persistent_shells",
        },
    )

    assert response.status_code == 200
    assert "todos" in response.json()["structuredContent"]


def test_http_tool_missing_required_arg_returns_validation_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
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

        assert response.status_code == 200
        assert response.json()["isError"] is True
        assert "session_id" in response.json()["content"][0]["text"]


def test_http_get_query_params_are_type_coerced(tmp_path, monkeypatch):
    (tmp_path / "artifact.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_MODE", "http")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    import workgate.control.downloads as download_ops

    clock = {"now": 1_000.0}
    monkeypatch.setattr(download_ops, "_now_s", lambda: clock["now"])

    client = TestClient(_build_paired_surface_http_app())
    session = client.post("/tools/session_start", json={"workdir": "."}).json()[
        "structuredContent"
    ]
    create_response = client.post(
        "/tools/create_file_link",
        json={
            "session_id": session["session_id"],
            "path": "artifact.txt",
            "ttl_s": 10,
        },
    )
    assert create_response.status_code == 200

    clock["now"] = 1_020.0
    include_response = client.post(
        "/tools/list_file_links",
        json={
            "session_id": session["session_id"],
            "include_expired": True,
        },
    )
    assert include_response.status_code == 200
    assert len(include_response.json()["structuredContent"]["links"]) == 1

    prune_response = client.post(
        "/tools/list_file_links",
        json={
            "session_id": session["session_id"],
            "include_expired": False,
        },
    )
    assert prune_response.status_code == 200
    assert prune_response.json()["structuredContent"]["links"] == []


def test_todos_are_task_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    client = TestClient(_build_paired_surface_http_app())
    first = client.post("/tools/task", json={"action": "create"}).json()[
        "structuredContent"
    ]["result"]["task_id"]
    second = client.post("/tools/task", json={"action": "create"}).json()[
        "structuredContent"
    ]["result"]["task_id"]
    first_items = [{"id": "first", "content": "one"}]
    second_items = [{"id": "second", "content": "two"}]

    assert (
        client.post(
            "/tools/write_todos",
            json={
                "task_id": first,
                "todos": first_items,
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/tools/write_todos",
            json={
                "task_id": second,
                "todos": second_items,
            },
        ).status_code
        == 200
    )

    first_payload = client.post(
        "/tools/read_todos", json={"task_id": first}
    ).json()["structuredContent"]
    second_payload = client.post(
        "/tools/read_todos", json={"task_id": second}
    ).json()["structuredContent"]
    assert first_payload["todos"][0]["id"] == "first"
    assert first_payload["todos"][0]["content"] == "one"
    assert second_payload["todos"][0]["id"] == "second"
    assert second_payload["todos"][0]["content"] == "two"


def test_http_tool_file_not_found_returns_json_error(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    client = TestClient(_build_paired_surface_http_app())
    session = client.post("/tools/session_start", json={"workdir": "."}).json()[
        "structuredContent"
    ]
    response = client.post(
        "/tools/read",
        json={"session_id": session["session_id"], "path": "missing.txt"},
    )

    assert response.status_code == 200
    assert response.json()["isError"] is True


def test_http_mode_hides_remote_worker_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_MODE", "http")
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    response = TestClient(build_http_app()).post(
        "/tools/run_remote_shell_command", json={"command": "echo ok"}
    )

    assert response.status_code == 200
    assert response.json()["isError"] is True
    assert "Unknown tool" in response.json()["content"][0]["text"]


@pytest.mark.asyncio
async def test_mcp_tool_missing_required_arg_uses_fastmcp_tool_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    with pytest.raises(ToolError, match="validation errors for readArguments"):
        await build_mcp().call_tool("read", {})


@pytest.mark.asyncio
async def test_mcp_remote_facade_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
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
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    with pytest.raises(ToolError, match="Unknown tool: no_such_tool"):
        await build_mcp().call_tool("no_such_tool", {})


@pytest.mark.asyncio
async def test_run_python_code_creates_temp_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()

    client = TestClient(_build_paired_surface_http_app())
    session_id = client.post(
        "/tools/session_start", json={"workdir": "."}
    ).json()["structuredContent"]["session_id"]
    payload = client.post(
        "/tools/run_python_code",
        json={
            "session_id": session_id,
            "code": "print('py314')",
            "cwd": ".",
        },
    ).json()["structuredContent"]

    assert payload["mode"] == "command"
    assert payload["cwd"] == str(tmp_path)
    assert payload["result"]["ok"] is True
    assert payload["result"]["stdout"].splitlines() == ["py314"]
    assert payload["script_path"].endswith(".py")
