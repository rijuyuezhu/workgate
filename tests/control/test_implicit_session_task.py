"""Session start creates exactly one durable Task before offering Executor work."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from tests.support.paired import (
    build_paired_control_harness,
    build_paired_http_app,
)
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.executor_transport import ExecutorTransportError
from workgate.oauth.core.context import bind_oauth_claims, reset_oauth_claims
from workgate.protocol.errors import ProtocolErrorCode
from workgate.protocol.executor import (
    SESSION_CREATE_OP,
    ExecutorResult,
    OperationError,
)
from workgate.protocol.ids import new_command_id


def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    clear_settings_cache()
    (tmp_path / "project").mkdir()
    return build_paired_control_harness(get_settings())


def _state(harness):
    return harness.control.control_state.snapshot_sessions()


@pytest.mark.asyncio
async def test_session_start_implicit_task_and_explicit_reuse(
    tmp_path, monkeypatch
):
    harness = setup(monkeypatch, tmp_path)
    coordinator = harness.control.session_coordinator
    service = harness.control.task_service

    created = await coordinator.start_session(
        workdir="project",
        label="Explore traces",
        executor_id=harness.executor_id,
    )
    assert isinstance(created, dict)
    task_id = created["task_id"]
    session_id = created["session_id"]
    assert isinstance(task_id, str) and task_id.startswith("task_")
    assert isinstance(session_id, str)
    assert isinstance(created["message"], str)
    assert created["task_created"] is True
    assert (
        f"Created Task {task_id} automatically and attached this Session"
        in created["message"]
    )
    assert "Use this session_id" in created["message"]
    task = await service.read_task(task_id)
    assert task.label == "Explore traces"
    assert task.objective is None
    assert task.session_ids == [session_id]
    assert _state(harness)[session_id].task_created is True
    assert harness.executor.sessions.lookup(session_id) is not None

    reused = await coordinator.start_session(
        workdir="project", executor_id=harness.executor_id, task_id=task_id
    )
    assert isinstance(reused, dict)
    assert isinstance(reused["message"], str)
    assert reused["task_created"] is False
    assert reused["task_id"] == task_id
    assert "Created Task" not in reused["message"]
    assert (await service.read_task(task_id)).session_ids == [
        session_id,
        reused["session_id"],
    ]
    assert len(await service.list_tasks()) == 1


@pytest.mark.asyncio
async def test_multibyte_session_label_can_exceed_task_label_bytes(
    tmp_path, monkeypatch
):
    harness = setup(monkeypatch, tmp_path)
    label = "标签" * 90
    started = await harness.control.session_coordinator.start_session(
        workdir="project", label=label, executor_id=harness.executor_id
    )
    assert isinstance(started, dict)
    task_id = started["task_id"]
    assert isinstance(task_id, str)
    assert started["label"] == label
    task = await harness.control.task_service.read_task(task_id)
    assert task.label is None
    assert task.session_ids == [started["session_id"]]


@pytest.mark.asyncio
async def test_implicit_task_owner_and_unauthorized_explicit_reuse(
    tmp_path, monkeypatch
):
    harness = setup(monkeypatch, tmp_path)
    coordinator = harness.control.session_coordinator
    alice = bind_oauth_claims({"sub": "alice"})
    try:
        created = await coordinator.start_session(
            workdir="project", executor_id=harness.executor_id
        )
    finally:
        reset_oauth_claims(alice)
    assert isinstance(created, dict)
    assert isinstance(created["task_id"], str)
    bob = bind_oauth_claims({"sub": "bob"})
    try:
        assert await harness.control.task_service.list_tasks() == []
        with pytest.raises(PermissionError, match="different principal"):
            await coordinator.start_session(
                workdir="project",
                executor_id=harness.executor_id,
                task_id=created["task_id"],
            )
        assert all(r.task_created for r in _state(harness).values())
    finally:
        reset_oauth_claims(bob)
    alice = bind_oauth_claims({"sub": "alice"})
    try:
        task = await harness.control.task_service.read_task(created["task_id"])
        assert task.session_ids == [created["session_id"]]
    finally:
        reset_oauth_claims(alice)


@pytest.mark.asyncio
async def test_capacity_or_executor_denial_does_not_create_task(
    tmp_path, monkeypatch
):
    harness = setup(monkeypatch, tmp_path)
    coordinator = harness.control.session_coordinator
    coordinator._max_agent_sessions = 1
    first = await coordinator.start_session(
        workdir="project", executor_id=harness.executor_id
    )
    assert isinstance(first, dict)
    assert isinstance(first["session_id"], str)
    with pytest.raises(RuntimeError, match="limit reached"):
        await coordinator.start_session(
            workdir="project", executor_id=harness.executor_id
        )
    assert len(await harness.control.task_service.list_tasks()) == 1
    await coordinator.end_session(first["session_id"])
    with pytest.raises(RuntimeError, match="not currently eligible"):
        await coordinator.start_session(
            workdir="project", executor_id="exec_missing"
        )
    assert len(await harness.control.task_service.list_tasks()) == 1


@pytest.mark.asyncio
async def test_executor_definite_failure_discards_implicit_task(
    tmp_path, monkeypatch
):
    harness = setup(monkeypatch, tmp_path)
    coordinator = harness.control.session_coordinator
    transport = harness.control.executor_transport

    async def refuse(_executor_id, op, *_args, **_kwargs):
        assert op == SESSION_CREATE_OP
        return ExecutorResult(
            id=new_command_id(),
            ok=False,
            error=OperationError(code="invalid_workdir", message="rejected"),
        )

    monkeypatch.setattr(transport, "call", refuse)
    with pytest.raises(RuntimeError, match="invalid_workdir"):
        await coordinator.start_session(
            workdir="project", executor_id=harness.executor_id
        )
    assert not _state(harness)
    assert await harness.control.task_service.list_tasks() == []


@pytest.mark.asyncio
async def test_not_offered_error_discards_implicit_task(tmp_path, monkeypatch):
    harness = setup(monkeypatch, tmp_path)

    async def failed(_executor_id, op, *_args, **_kwargs):
        assert op == SESSION_CREATE_OP
        raise ExecutorTransportError(
            ProtocolErrorCode.EXECUTOR_OFFLINE,
            "not offered",
            delivery_state="queued",
        )

    monkeypatch.setattr(harness.control.executor_transport, "call", failed)
    with pytest.raises(ExecutorTransportError, match="not offered"):
        await harness.control.session_coordinator.start_session(
            workdir="project", executor_id=harness.executor_id
        )
    assert not _state(harness)
    assert await harness.control.task_service.list_tasks() == []


@pytest.mark.asyncio
async def test_ambiguous_create_retains_attached_task_and_retry_has_new_identity(
    tmp_path, monkeypatch
):
    harness = setup(monkeypatch, tmp_path)
    coordinator = harness.control.session_coordinator
    transport = harness.control.executor_transport
    original_call = transport.call
    offered = []

    async def ambiguous(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        if op == SESSION_CREATE_OP and not offered:
            offered.append(session_id)
            raise ExecutorTransportError(
                ProtocolErrorCode.EXECUTOR_OFFLINE,
                "offer timed out",
                delivery_state="offered",
            )
        return await original_call(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )

    monkeypatch.setattr(transport, "call", ambiguous)
    with pytest.raises(ExecutorTransportError, match="offer timed out"):
        await coordinator.start_session(
            workdir="project", executor_id=harness.executor_id
        )
    first = _state(harness)[offered[0]]
    assert first.task_created and first.task_id is not None
    assert first.status == "creating"
    assert (
        await harness.control.task_service.read_task(str(first.task_id))
    ).session_ids == [offered[0]]
    retried = await coordinator.start_session(
        workdir="project", executor_id=harness.executor_id
    )
    assert isinstance(retried, dict)
    assert retried["task_id"] != first.task_id
    assert retried["session_id"] != first.session_id
    assert len(await harness.control.task_service.list_tasks()) == 2


@pytest.mark.asyncio
async def test_http_response_and_webui_expose_implicit_notice(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    (tmp_path / "project").mkdir()
    app, harness = build_paired_http_app(get_settings())
    with TestClient(app, base_url="https://workgate.example") as client:
        response = client.post(
            "/tools/session_start",
            json={"workdir": "project", "label": "http-auto"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["isError"] is False
        started = data["structuredContent"]
        assert started["task_created"] is True
        assert started["task_id"].startswith("task_")
        assert started["task_id"] in started["message"]
        assert any(
            started["task_id"] in part.get("text", "")
            for part in data["content"]
        )
        events = [
            json.loads(line)
            for line in get_settings().audit_log_path.read_text().splitlines()
        ]
        assert any(
            e.get("event") == "task_mutation"
            and e.get("operation") == "create"
            and e.get("task") == started["task_id"]
            for e in events
        )
        assert any(
            e.get("event") == "tool_call_end"
            and e.get("tool") == "session_start"
            and e.get("output", {}).get("task_id") == started["task_id"]
            for e in events
        )
        detail = client.get(
            "/api/ui/tasks/detail", params={"task_id": started["task_id"]}
        )
        assert detail.status_code == 200
        sessions = detail.json()["data"]["task"]["sessions"]
        assert sessions[0]["task_created"] is True
        assert sessions[0]["task_id"] == started["task_id"]


@pytest.mark.asyncio
async def test_cannot_detach_new_implicit_task_during_session_create(
    tmp_path, monkeypatch
):
    harness = setup(monkeypatch, tmp_path)
    service = harness.control.task_service
    transport = harness.control.executor_transport
    original_call = transport.call
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        if op == SESSION_CREATE_OP:
            entered.set()
            await release.wait()
        return await original_call(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )

    monkeypatch.setattr(transport, "call", delayed)
    pending = asyncio.create_task(
        harness.control.session_coordinator.start_session(
            workdir="project", executor_id=harness.executor_id
        )
    )
    await entered.wait()
    record = next(iter(_state(harness).values()))
    assert record.status == "creating" and record.task_created
    task_id = str(record.task_id)
    await service.cancel_task(task_id)
    with pytest.raises(ValueError, match="while its Session is creating"):
        await service.delete_task(task_id)
    assert _state(harness)[str(record.session_id)].task_id == task_id
    release.set()
    started = await pending
    assert isinstance(started, dict) and started["task_id"] == task_id
    await service.delete_task(task_id)
    detached = _state(harness)[str(record.session_id)]
    assert detached.task_id is None and detached.task_created is False


@pytest.mark.asyncio
async def test_task_admission_failure_cleans_up_new_task(tmp_path, monkeypatch):
    harness = setup(monkeypatch, tmp_path)
    service = harness.control.task_service

    def reject(_record):
        raise RuntimeError("task session admission rejected")

    monkeypatch.setattr(service, "admit_session_attachment", reject)
    with pytest.raises(RuntimeError, match="task session admission rejected"):
        await harness.control.session_coordinator.start_session(
            workdir="project", executor_id=harness.executor_id
        )
    assert await service.list_tasks() == []
    assert not _state(harness)


@pytest.mark.asyncio
async def test_queued_timeout_cleans_up_new_task(tmp_path, monkeypatch):
    harness = setup(monkeypatch, tmp_path)

    async def timeout(_executor_id, op, *_args, **_kwargs):
        assert op == SESSION_CREATE_OP
        exc = TimeoutError("queued timeout")
        exc._workgate_executor_delivery_state = "queued"  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(harness.control.executor_transport, "call", timeout)
    with pytest.raises(TimeoutError, match="queued timeout"):
        await harness.control.session_coordinator.start_session(
            workdir="project", executor_id=harness.executor_id
        )
    assert await harness.control.task_service.list_tasks() == []
    assert not _state(harness)
