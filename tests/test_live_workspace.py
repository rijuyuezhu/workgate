from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import BaseModel

from tests.helpers import build_paired_control_harness
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.mcp import live_workspace as live
from workgate.control.mcp.app import build_mcp


def _started_session_id(value: Any) -> str:
    assert isinstance(value, dict)
    session_id = value.get("session_id")
    assert isinstance(session_id, str)
    return session_id


def _http_settings(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKGATE_MODE", "http")
    monkeypatch.setenv("WORKGATE_UI_ENABLED", "true")
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://workgate.example.test")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    return get_settings()


@pytest.mark.asyncio
async def test_live_workspace_mcp_app_metadata_is_session_scoped(
    tmp_path, monkeypatch
):
    settings = _http_settings(tmp_path, monkeypatch)
    harness = build_paired_control_harness(settings)
    mcp = build_mcp(runtime=harness.control)

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "workspace_open" in tools
    assert tools["workspace_open"].meta is not None
    open_meta = tools["workspace_open"].meta
    assert open_meta["ui/resourceUri"].startswith(
        "ui://workgate/live-workspace-"
    )
    assert open_meta["openai/outputTemplate"] == open_meta["ui/resourceUri"]
    assert open_meta["openai/widgetAccessible"] is True
    assert open_meta["securitySchemes"][0]["scopes"] == [
        "shell:read",
        "audit:read",
    ]

    for name in (
        "workspace_snapshot",
        "workspace_task_control",
        "workspace_end",
    ):
        meta = tools[name].meta
        assert meta is not None
        assert meta["ui"]["visibility"] == ["app"]
    control_meta = tools["workspace_task_control"].meta
    assert control_meta is not None
    assert control_meta["securitySchemes"][0]["scopes"] == [
        "shell:read",
        "shell:write",
        "audit:read",
    ]
    assert tools["workspace_task_control"].annotations is not None
    assert tools["workspace_task_control"].annotations.destructiveHint is True
    assert tools["workspace_end"].annotations is not None
    assert tools["workspace_end"].annotations.destructiveHint is True

    resources = {
        str(resource.uri): resource for resource in await mcp.list_resources()
    }
    assert "ui://workgate/live-workspace.html" in resources
    assert open_meta["ui/resourceUri"] in resources
    assert (
        resources[open_meta["ui/resourceUri"]].mimeType
        == "text/html;profile=mcp-app"
    )


@pytest.mark.asyncio
async def test_live_workspace_snapshot_reuses_explicit_session_and_redacts_activity(
    tmp_path, monkeypatch
):
    settings = _http_settings(tmp_path, monkeypatch)
    harness = build_paired_control_harness(settings)
    started = await harness.control.session_coordinator.start_session(
        workdir=".", label="review me"
    )
    session_id = _started_session_id(started)
    await harness.control.todo_service.write(
        session_id,
        [{"id": "one", "content": "inspect", "status": "in_progress"}],
    )

    monkeypatch.setattr(
        live,
        "query_audit",
        lambda **_kwargs: {
            "entries": [
                {
                    "id": "audit-1",
                    "ts": 123.0,
                    "event": "tool_call",
                    "tool": "bash",
                    "operation": "execute",
                    "ok": True,
                    "duration_ms": 7,
                    "args": {"command": "printf super-secret"},
                    "result": {"stdout": "super-secret"},
                    "token": "super-secret",
                }
            ]
        },
    )

    snapshot = await live.live_workspace_snapshot(harness.control, session_id)
    data = snapshot.model_dump(mode="json")

    assert data["session"]["session_id"] == session_id
    assert data["session"]["executor_id"] == harness.executor_id
    assert data["session"]["label"] == "review me"
    assert data["session"]["availability"] == "available"
    assert data["task"] is None
    assert data["task_controls_available"] is False
    assert data["compatibility_plan"][0]["content"] == "inspect"
    assert data["activity"] == [
        {
            "id": "audit-1",
            "ts": 123.0,
            "event": "tool_call",
            "tool": "bash",
            "operation": "execute",
            "ok": True,
            "duration_ms": 7.0,
        }
    ]
    assert "super-secret" not in str(data)
    for view in ("sessions", "files", "terminals", "audit"):
        parsed = urlparse(data["links"][view])
        assert parsed.scheme == "https"
        assert parsed.netloc == "workgate.example.test"
        assert parsed.path == "/ui"
        assert parsed.fragment == view
        query = parse_qs(parsed.query)
        assert query["session_id"] == [session_id]
        assert query["executor_id"] == [harness.executor_id]
    assert parse_qs(urlparse(data["links"]["files"]).query)["workdir"] == ["."]
    assert "shell_id" not in parse_qs(
        urlparse(data["links"]["terminals"]).query
    )

    with pytest.raises(ValueError, match="unknown session_id"):
        await live.live_workspace_snapshot(harness.control, "sess_00000000")


class _FakeTask(BaseModel):
    revision: int = 4
    objective: str | None = "ship Live Workspace"
    status: str = "active"
    progress: dict[str, Any] = {}
    plan: dict[str, Any] = {"steps": []}


class _TaskService:
    def __init__(self) -> None:
        self.task = _FakeTask()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def read_task(self, _session_id: str) -> _FakeTask:
        return self.task

    async def report_progress(
        self, session_id: str, **kwargs: Any
    ) -> _FakeTask:
        self.calls.append((session_id, kwargs))
        if "task_status" in kwargs:
            self.task.status = kwargs["task_status"]
        if "next_action" in kwargs:
            self.task.progress = {"next_action": kwargs["next_action"]}
        self.task.revision += 1
        return self.task


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
    actions, _message = live._task_control_actions(
        {"status": task_status},
        execution_status="active",
        service_available=True,
    )
    assert actions == expected

    ended_actions, ended_message = live._task_control_actions(
        {"status": task_status},
        execution_status="ended",
        service_available=True,
    )
    assert ended_actions == []
    assert ended_message is not None


@pytest.mark.asyncio
async def test_live_workspace_controls_use_canonical_task_service(
    tmp_path, monkeypatch
):
    settings = _http_settings(tmp_path, monkeypatch)
    harness = build_paired_control_harness(settings)
    started = await harness.control.session_coordinator.start_session(
        workdir="."
    )
    session_id = _started_session_id(started)
    service = _TaskService()
    harness.control.todo_service = service  # type: ignore[assignment]
    monkeypatch.setattr(live, "query_audit", lambda **_kwargs: {"entries": []})

    blocked = await live.live_workspace_task_control(
        harness.control,
        session_id=session_id,
        action="block",
        expected_revision=4,
    )
    assert service.calls[-1] == (
        session_id,
        {"expected_revision": 4, "task_status": "blocked"},
    )
    assert blocked.task is not None
    assert blocked.task["status"] == "blocked"
    assert blocked.task_controls_available is True
    assert blocked.task_control_actions == [
        "resume",
        "cancel",
        "next_instruction",
    ]

    with pytest.raises(ValueError, match="not available"):
        await live.live_workspace_task_control(
            harness.control,
            session_id=session_id,
            action="block",
            expected_revision=5,
        )
    assert len(service.calls) == 1

    instructed = await live.live_workspace_task_control(
        harness.control,
        session_id=session_id,
        action="next_instruction",
        expected_revision=5,
        instruction="Please inspect the failing browser test.",
    )
    assert service.calls[-1][1]["next_action"] == (
        "Please inspect the failing browser test."
    )
    assert instructed.task is not None
    assert instructed.task["progress"]["next_action"].startswith(
        "Please inspect"
    )

    with pytest.raises(ValueError, match="instruction is required"):
        await live.live_workspace_task_control(
            harness.control,
            session_id=session_id,
            action="next_instruction",
            expected_revision=6,
            instruction=" ",
        )


@pytest.mark.asyncio
async def test_live_workspace_end_requires_exact_separate_confirmation(
    tmp_path, monkeypatch
):
    settings = _http_settings(tmp_path, monkeypatch)
    harness = build_paired_control_harness(settings)
    started = await harness.control.session_coordinator.start_session(
        workdir="."
    )
    session_id = _started_session_id(started)

    with pytest.raises(ValueError, match="exactly match"):
        await live.live_workspace_end(
            harness.control,
            session_id=session_id,
            confirm_session_id="sess_wrong",
        )
    assert (
        harness.control.control_state.snapshot_sessions()[session_id].status
        == "active"
    )

    ended = await live.live_workspace_end(
        harness.control,
        session_id=session_id,
        confirm_session_id=session_id,
    )
    assert ended.ended is True
    assert (
        harness.control.control_state.snapshot_sessions()[session_id].status
        == "ended"
    )


@pytest.mark.asyncio
async def test_live_workspace_snapshot_survives_executor_offline(
    tmp_path, monkeypatch
):
    settings = _http_settings(tmp_path, monkeypatch)
    harness = build_paired_control_harness(settings)
    started = await harness.control.session_coordinator.start_session(
        workdir="."
    )
    session_id = _started_session_id(started)
    monkeypatch.setattr(live, "query_audit", lambda **_kwargs: {"entries": []})

    async def offline(_executor_id: str) -> bool:
        return False

    harness.control.executor_transport.is_online = offline  # type: ignore[method-assign]
    snapshot = await live.live_workspace_snapshot(harness.control, session_id)

    assert snapshot.session.session_id == session_id
    assert snapshot.session.availability == "executor_offline"
    assert snapshot.shells == []
    assert snapshot.shells_message is not None
    assert "executor_offline" in snapshot.shells_message
