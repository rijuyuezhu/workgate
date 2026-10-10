from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.mcpserver.exceptions import ResourceError

from tests.helpers import build_paired_control_harness
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.mcp import live_workspace as live
from workgate.control.mcp.app import build_mcp
from workgate.schemas.result_models.task import TaskDocument


def _started_session_id(value: Any) -> str:
    assert isinstance(value, dict)
    session_id = value.get("session_id")
    assert isinstance(session_id, str)
    return session_id


def _http_settings(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(workspace))
    monkeypatch.setenv("WORKGATE_MODE", "http")
    monkeypatch.setenv("WORKGATE_UI_ENABLED", "true")
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://workgate.example.test")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    return get_settings()


async def _task_session(harness, *, label: str = "review me"):
    task = await harness.control.task_service.create_task(
        label="workspace task"
    )
    started = await harness.control.session_coordinator.start_session(
        workdir=".",
        label=label,
        executor_id=harness.executor_id,
        task_id=task.task_id,
    )
    return task.task_id, _started_session_id(started)


@pytest.mark.asyncio
async def test_live_workspace_mcp_app_metadata_is_task_scoped(
    tmp_path, monkeypatch
):
    settings = _http_settings(tmp_path, monkeypatch)
    harness = build_paired_control_harness(settings)
    mcp = build_mcp(runtime=harness.control)

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "workspace_open" in tools
    open_tool = tools["workspace_open"]
    assert open_tool.meta is not None
    assert "task_id" in open_tool.input_schema["properties"]
    assert "task_id" in open_tool.input_schema["required"]
    assert "session_id" in open_tool.input_schema["properties"]
    assert "session_id" not in open_tool.input_schema["required"]

    open_meta = open_tool.meta
    assert open_meta["ui/resourceUri"].startswith(
        "ui://workgate/live-workspace-"
    )
    assert open_meta["openai/outputTemplate"] == open_meta["ui/resourceUri"]
    assert open_meta["openai/widgetAccessible"] is True
    assert "visibility" not in open_meta["ui"]
    assert open_meta["securitySchemes"][0]["scopes"] == [
        "shell:read",
        "audit:read",
    ]

    for name in (
        "workspace_snapshot",
        "workspace_task_control",
        "workspace_continuation",
        "workspace_end",
    ):
        meta = tools[name].meta
        assert meta is not None
        assert meta["ui"]["visibility"] == ["app"]

    resources = {
        str(resource.uri): resource for resource in await mcp.list_resources()
    }
    assert "ui://workgate/live-workspace.html" in resources
    templates = {
        template.uri_template
        for template in await mcp.list_resource_templates()
    }
    assert "ui://workgate/live-workspace-{digest}.html" in templates
    for uri in (
        open_meta["ui/resourceUri"],
        "ui://workgate/live-workspace-0123456789abcdef.html",
    ):
        result = await mcp.read_resource(uri)
        assert "Workgate Live Workspace" in str(result)
    with pytest.raises(
        ResourceError, match="Invalid Live Workspace resource cache key"
    ):
        await mcp.read_resource("ui://workgate/live-workspace-invalid.html")


@pytest.mark.asyncio
async def test_mcp_activity_uses_tool_visibility_not_workspace_name(
    tmp_path, monkeypatch
):
    settings = _http_settings(tmp_path, monkeypatch)
    harness = build_paired_control_harness(settings)
    task = await harness.control.task_service.create_task(
        label="activity metadata"
    )
    observed: list[tuple[str, ...]] = []

    async def observe(task_ids: tuple[str, ...]) -> None:
        observed.append(task_ids)

    monkeypatch.setattr(
        harness.control.task_service,
        "observe_agent_activity",
        observe,
    )
    mcp = build_mcp(runtime=harness.control)

    await mcp.call_tool("workspace_snapshot", {"task_id": task.task_id})
    assert observed == []

    await mcp.call_tool("workspace_open", {"task_id": task.task_id})
    assert observed == [(task.task_id,)]


@pytest.mark.asyncio
async def test_live_workspace_is_task_first_and_never_selects_session_implicitly(
    tmp_path, monkeypatch
):
    settings = _http_settings(tmp_path, monkeypatch)
    harness = build_paired_control_harness(settings)
    task_id, session_id = await _task_session(harness)
    await harness.control.task_service.update_plan(
        task_id,
        steps=[{"id": "one", "content": "inspect", "status": "in_progress"}],
    )

    audit_queries = []

    def fake_query_audit(**kwargs):
        audit_queries.append(kwargs)
        return {
            "entries": [
                {
                    "id": "refresh-noise",
                    "ts": 124.0,
                    "event": "tool_call",
                    "tool": "workspace_snapshot",
                    "operation": "other",
                    "task": task_id,
                    "session": session_id,
                    "ok": True,
                    "duration_ms": 1,
                },
                {
                    "id": "audit-1",
                    "ts": 123.0,
                    "event": "tool_call",
                    "tool": "bash",
                    "operation": "shell",
                    "task": task_id,
                    "session": session_id,
                    "ok": True,
                    "duration_ms": 7,
                    "args": {"command": "printf super-secret"},
                },
            ]
        }

    monkeypatch.setattr(live, "query_audit", fake_query_audit)

    task_only = await live.live_workspace_snapshot(harness.control, task_id)
    assert task_only.task.task_id == task_id
    assert task_only.session is None
    assert [item.session_id for item in task_only.sessions] == [session_id]
    assert task_only.jobs == []
    assert task_only.shells == []
    assert task_only.links is None

    selected = await live.live_workspace_snapshot(
        harness.control, task_id, session_id
    )
    assert selected.session is not None
    assert selected.session.session_id == session_id
    assert selected.session.executor_id == harness.executor_id
    assert selected.task.plan.steps[0].content == "inspect"
    assert selected.task_control_actions == [
        "block",
        "cancel",
        "next_instruction",
    ]
    assert selected.activity[0].id == "audit-1"
    assert selected.activity[0].session == session_id
    assert "super-secret" not in str(selected.model_dump(mode="json"))
    assert audit_queries[0]["task"] == task_id
    assert "session" not in audit_queries[0]

    assert selected.links is not None
    links = selected.links.model_dump()
    for view in ("tasks", "files", "terminals", "audit"):
        parsed = urlparse(links[view])
        assert parsed.scheme == "https"
        assert parsed.netloc == "workgate.example.test"
        assert parsed.path == "/ui"
        assert parsed.fragment == view
        query = parse_qs(parsed.query)
        assert query["task_id"] == [task_id]
        assert query["session_id"] == [session_id]
        assert query["executor_id"] == [harness.executor_id]

    other = await harness.control.task_service.create_task(label="other")
    with pytest.raises(ValueError, match="not attached"):
        await live.live_workspace_snapshot(
            harness.control, other.task_id, session_id
        )


@pytest.mark.asyncio
async def test_live_workspace_job_message_does_not_forward_backend_exception_text(
    tmp_path, monkeypatch
):
    settings = _http_settings(tmp_path, monkeypatch)
    harness = build_paired_control_harness(settings)

    class _JobOutput:
        jobs: list[Any] = []
        message = (
            "Executor jobs unavailable: RuntimeError: backend-super-secret"
        )

    calls: list[dict[str, Any]] = []

    async def secret_job_list(**kwargs: Any) -> _JobOutput:
        calls.append(kwargs)
        return _JobOutput()

    monkeypatch.setattr(harness.control.job_service, "execute", secret_job_list)
    jobs, message = await live._job_projection(
        harness.control, "sess_0000000000000000000001", "active"
    )

    assert jobs == []
    assert message == "Some executor job metadata is unavailable."
    assert "backend-super-secret" not in message


@pytest.mark.parametrize(
    ("task_status", "expected"),
    [
        ("active", ["block", "cancel", "next_instruction"]),
        ("blocked", ["resume", "cancel", "next_instruction"]),
        ("completed", ["resume"]),
        ("cancelled", []),
    ],
)
def test_live_workspace_task_actions_follow_task_lifecycle(
    task_status, expected
):
    task = TaskDocument.model_validate(
        {
            "created_at": 1.0,
            "updated_at": 1.0,
            "status": task_status,
        }
    )
    actions, _message = live._task_control_actions(task)
    assert actions == expected


@pytest.mark.asyncio
async def test_live_workspace_controls_mutate_task_independently_of_session(
    tmp_path, monkeypatch
):
    settings = _http_settings(tmp_path, monkeypatch)
    harness = build_paired_control_harness(settings)
    task_id, session_id = await _task_session(harness)
    monkeypatch.setattr(live, "query_audit", lambda **_kwargs: {"entries": []})

    blocked = await live.live_workspace_task_control(
        harness.control,
        task_id=task_id,
        session_id=session_id,
        action="block",
    )
    assert blocked.task.status == "blocked"
    assert blocked.task_control_actions == [
        "resume",
        "cancel",
        "next_instruction",
    ]

    instructed = await live.live_workspace_task_control(
        harness.control,
        task_id=task_id,
        session_id=None,
        action="next_instruction",
        instruction="Please inspect the failing browser test.",
    )
    assert instructed.session is None
    assert instructed.task.progress.next_action is not None
    assert instructed.task.progress.next_action.startswith("Please inspect")

    with pytest.raises(ValueError, match="instruction is required"):
        await live.live_workspace_task_control(
            harness.control,
            task_id=task_id,
            session_id=None,
            action="next_instruction",
            instruction=" ",
        )


@pytest.mark.asyncio
async def test_live_workspace_end_requires_task_attachment_and_confirmation(
    tmp_path, monkeypatch
):
    settings = _http_settings(tmp_path, monkeypatch)
    harness = build_paired_control_harness(settings)
    task_id, session_id = await _task_session(harness)
    other = await harness.control.task_service.create_task(label="other")

    with pytest.raises(ValueError, match="exactly match"):
        await live.live_workspace_end(
            harness.control,
            task_id=task_id,
            session_id=session_id,
            confirm_session_id="sess_wrong",
        )
    with pytest.raises(ValueError, match="not attached"):
        await live.live_workspace_end(
            harness.control,
            task_id=other.task_id,
            session_id=session_id,
            confirm_session_id=session_id,
        )

    ended = await live.live_workspace_end(
        harness.control,
        task_id=task_id,
        session_id=session_id,
        confirm_session_id=session_id,
    )
    assert ended.ended is True
    task = await harness.control.task_service.report_progress(
        task_id,
        summary="Still mutable after execution ended",
    )
    assert task.progress.summary == "Still mutable after execution ended"


@pytest.mark.asyncio
async def test_live_workspace_selected_session_survives_executor_offline(
    tmp_path, monkeypatch
):
    settings = _http_settings(tmp_path, monkeypatch)
    harness = build_paired_control_harness(settings)
    task_id, session_id = await _task_session(harness)
    monkeypatch.setattr(live, "query_audit", lambda **_kwargs: {"entries": []})

    async def offline(_executor_id: str) -> bool:
        return False

    harness.control.executor_transport.is_online = offline  # type: ignore[method-assign]
    snapshot = await live.live_workspace_snapshot(
        harness.control, task_id, session_id
    )

    assert snapshot.session is not None
    assert snapshot.session.session_id == session_id
    assert snapshot.session.availability == "executor_offline"
    assert snapshot.shells == []
    assert snapshot.shells_message is not None
    assert "executor_offline" in snapshot.shells_message


@pytest.mark.asyncio
async def test_live_workspace_continuation_is_task_only_and_bounded(
    tmp_path, monkeypatch
):
    settings = _http_settings(tmp_path, monkeypatch)
    harness = build_paired_control_harness(settings)
    task = await harness.control.task_service.create_task(
        label="continue task", objective="Keep going"
    )
    planned = await harness.control.task_service.update_plan(
        task.task_id,
        steps=[
            {
                "id": "remaining",
                "content": "finish remaining work",
                "status": "in_progress",
            }
        ],
    )
    clock = [planned.updated_at + 901]
    monkeypatch.setattr(
        "workgate.control.task_state.time.time", lambda: clock[0]
    )

    claimed = await live.live_workspace_continuation(
        harness.control,
        task_id=task.task_id,
        action="claim",
    )
    assert claimed.claimed is True
    assert claimed.task.task_id == task.task_id
    assert claimed.task.session_ids == []
    assert claimed.continuation.pending is True

    claim_id = claimed.claim_id
    assert claim_id is not None

    competing = await live.live_workspace_continuation(
        harness.control,
        task_id=task.task_id,
        action="claim",
    )
    assert competing.claimed is False

    validated = await live.live_workspace_continuation(
        harness.control,
        task_id=task.task_id,
        action="validate",
        claim_id=claim_id,
    )
    assert validated.valid is True
    assert validated.continuation.attempt_count == 1

    reported = await live.live_workspace_continuation(
        harness.control,
        task_id=task.task_id,
        action="report",
        claim_id=claim_id,
        accepted=False,
    )
    assert reported.reported is True
    assert reported.accepted is False
    assert reported.continuation.pending is False
    assert reported.continuation.due_at == clock[0] + 300


@pytest.mark.asyncio
async def test_mcp_task_activity_invalidates_pending_continuation(
    tmp_path, monkeypatch
):
    settings = _http_settings(tmp_path, monkeypatch)
    harness = build_paired_control_harness(settings)
    service = harness.control.task_service
    task = await service.create_task(label="agent activity")
    planned = await service.update_plan(
        task.task_id,
        steps=[{"id": "one", "content": "continue", "status": "in_progress"}],
    )
    clock = [planned.updated_at + 901]
    monkeypatch.setattr(
        "workgate.control.task_state.time.time", lambda: clock[0]
    )
    claimed = await service.claim_continuation(task.task_id)
    assert claimed["claimed"] is True
    claim_id = claimed["claim_id"]
    assert isinstance(claim_id, str)

    clock[0] += 1
    mcp = build_mcp(runtime=harness.control)
    await mcp.call_tool("task", {"action": "get", "task_id": task.task_id})

    stale = await service.validate_continuation(task.task_id, claim_id=claim_id)
    assert stale["valid"] is False
    status = await service.continuation_status(task.task_id)
    assert status["due_at"] == clock[0] + 900
