import asyncio
import json

import pytest

from tests.support.paired import build_paired_control_harness
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.runtime import build_control_runtime
from workgate.control.state import ExecutorTrustRecord
from workgate.oauth.core.context import bind_oauth_claims, reset_oauth_claims
from workgate.protocol.credentials import (
    executor_credential_verifier,
    new_executor_credential,
)
from workgate.protocol.executor import SESSION_CREATE_OP
from workgate.protocol.ids import new_executor_id


def _configure(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    clear_settings_cache()


def _harness(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    (tmp_path / "project-a").mkdir(parents=True, exist_ok=True)
    (tmp_path / "project-b").mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    return settings, build_paired_control_harness(settings)


async def _task_with_session(monkeypatch, tmp_path):
    settings, harness = _harness(monkeypatch, tmp_path)
    task = await harness.control.task_service.create_task(
        label="durable task", objective="Ship task identity"
    )
    started = await harness.control.session_coordinator.start_session(
        workdir="project-a",
        label="execution a",
        executor_id=harness.executor_id,
        task_id=task.task_id,
    )
    assert isinstance(started, dict)
    return settings, harness, task.task_id, str(started["session_id"])


@pytest.mark.asyncio
async def test_task_can_exist_without_execution_session(tmp_path, monkeypatch):
    _settings, harness = _harness(monkeypatch, tmp_path)

    created = await harness.control.task_service.create_task(
        label="semantic work", objective="Plan before choosing a machine"
    )

    assert created.task_id.startswith("task_")
    assert created.session_ids == []
    assert created.status == "active"
    assert created.label == "semantic work"
    assert created.objective == "Plan before choosing a machine"


@pytest.mark.asyncio
async def test_multiple_execution_sessions_attach_to_one_task(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    service = harness.control.task_service
    task = await service.create_task(label="multi-session")

    first = await harness.control.session_coordinator.start_session(
        workdir="project-a",
        executor_id=harness.executor_id,
        task_id=task.task_id,
    )
    second = await harness.control.session_coordinator.start_session(
        workdir="project-b",
        executor_id=harness.executor_id,
        task_id=task.task_id,
    )
    assert isinstance(first, dict)
    assert isinstance(second, dict)

    current = await service.read_task(task.task_id)
    assert current.session_ids == [first["session_id"], second["session_id"]]
    records = harness.control.control_state.snapshot_sessions()
    assert str(records[str(first["session_id"])].task_id) == task.task_id
    assert str(records[str(second["session_id"])].task_id) == task.task_id


@pytest.mark.asyncio
async def test_one_task_can_span_distinct_executors_and_workdirs(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    task = await harness.control.task_service.create_task(
        label="cross-executor"
    )
    second_executor = str(new_executor_id())
    credential = new_executor_credential()
    harness.control.control_state.put_executor(
        ExecutorTrustRecord(
            executor_id=second_executor,
            name="second-executor",
            credential_verifier=executor_credential_verifier(credential),
            created_at=2.0,
        )
    )

    transport = harness.control.executor_transport
    original_call = harness.call
    original_inventory = harness.inventory

    async def is_online(executor_id: str) -> bool:
        return executor_id in {harness.executor_id, second_executor}

    async def inventory(executor_id: str):
        if executor_id not in {harness.executor_id, second_executor}:
            return None
        return await original_inventory(harness.executor_id)

    async def call(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        assert executor_id in {harness.executor_id, second_executor}
        return await original_call(
            harness.executor_id,
            op,
            args,
            session_id=session_id,
            timeout_s=timeout_s,
        )

    monkeypatch.setattr(transport, "is_online", is_online)
    monkeypatch.setattr(transport, "inventory", inventory)
    monkeypatch.setattr(transport, "call", call)

    first = await harness.control.session_coordinator.start_session(
        workdir="project-a",
        executor_id=harness.executor_id,
        task_id=task.task_id,
    )
    second = await harness.control.session_coordinator.start_session(
        workdir="project-b",
        executor_id=second_executor,
        task_id=task.task_id,
    )
    assert isinstance(first, dict)
    assert isinstance(second, dict)

    records = harness.control.control_state.snapshot_sessions()
    first_record = records[str(first["session_id"])]
    second_record = records[str(second["session_id"])]
    assert str(first_record.executor_id) == harness.executor_id
    assert str(second_record.executor_id) == second_executor
    assert first_record.workdir != second_record.workdir
    current = await harness.control.task_service.read_task(task.task_id)
    assert current.session_ids == [first["session_id"], second["session_id"]]


@pytest.mark.asyncio
async def test_control_session_task_binding_cannot_change_via_lifecycle_update(
    tmp_path, monkeypatch
):
    _settings, harness, _task_id, session_id = await _task_with_session(
        monkeypatch, tmp_path
    )
    state = harness.control.control_state
    current = state.snapshot_sessions()[session_id]

    other = await harness.control.task_service.create_task(label="other")
    rebound = current.model_copy(update={"task_id": other.task_id})
    with pytest.raises(
        ValueError, match="cannot change through lifecycle updates"
    ):
        state.put_session(rebound)


@pytest.mark.asyncio
async def test_session_start_does_not_attach_task_deleted_during_executor_selection(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    service = harness.control.task_service
    coordinator = harness.control.session_coordinator
    task = await service.create_task(label="delete during selection")
    selected = asyncio.Event()
    release = asyncio.Event()
    original_select = coordinator.select_executor

    async def delayed_select(executor_id=None):
        result = await original_select(executor_id)
        selected.set()
        await release.wait()
        return result

    monkeypatch.setattr(coordinator, "select_executor", delayed_select)
    starting = asyncio.create_task(
        coordinator.start_session(
            workdir="project-a",
            executor_id=harness.executor_id,
            task_id=task.task_id,
        )
    )
    await selected.wait()
    await service.cancel_task(task.task_id)
    await service.delete_task(task.task_id)
    release.set()

    with pytest.raises(ValueError, match="unknown task_id"):
        await starting
    assert all(
        str(record.task_id or "") != task.task_id
        for record in harness.control.control_state.snapshot_sessions().values()
    )


@pytest.mark.asyncio
async def test_task_delete_during_session_create_keeps_session_detached(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    service = harness.control.task_service
    coordinator = harness.control.session_coordinator
    task = await service.create_task(label="delete during create")
    create_sent = asyncio.Event()
    release = asyncio.Event()
    original_call = harness.control.executor_transport.call

    async def delayed_call(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        if op == SESSION_CREATE_OP:
            create_sent.set()
            await release.wait()
        return await original_call(
            executor_id,
            op,
            args,
            session_id=session_id,
            timeout_s=timeout_s,
        )

    monkeypatch.setattr(
        harness.control.executor_transport, "call", delayed_call
    )
    starting = asyncio.create_task(
        coordinator.start_session(
            workdir="project-a",
            executor_id=harness.executor_id,
            task_id=task.task_id,
        )
    )
    await create_sent.wait()
    attached = [
        record
        for record in harness.control.control_state.snapshot_sessions().values()
        if str(record.task_id or "") == task.task_id
    ]
    assert len(attached) == 1
    session_id = str(attached[0].session_id)

    await service.cancel_task(task.task_id)
    await service.delete_task(task.task_id)
    assert (
        harness.control.control_state.snapshot_sessions()[session_id].task_id
        is None
    )

    release.set()
    result = await starting
    current = harness.control.control_state.snapshot_sessions()[session_id]
    assert current.status == "active"
    assert current.task_id is None
    assert isinstance(result, dict)
    assert result.get("task_id") is None


@pytest.mark.asyncio
async def test_task_progress_plan_and_todos_share_one_document(
    tmp_path, monkeypatch
):
    _settings, harness, task_id, _session_id = await _task_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.task_service

    initial = await service.read_task(task_id)
    assert initial.plan.steps == []

    reported = await service.report_progress(
        task_id,
        summary="Mapped the execution boundary",
        findings=["Task state is control-owned"],
        next_action="Add a structured plan",
        blockers=[],
    )
    assert reported.progress.summary == "Mapped the execution boundary"

    planned = await service.update_plan(
        task_id,
        steps=[
            {
                "id": "model",
                "content": "Implement canonical task state",
                "status": "completed",
                "priority": "high",
            },
            {
                "id": "ui",
                "content": "Expose task sessions",
                "status": "in_progress",
                "priority": "medium",
            },
        ],
    )
    assert planned.progress.summary == "Mapped the execution boundary"

    todos = await service.read(task_id)
    assert [(item.id, item.status) for item in todos.todos] == [
        ("model", "completed"),
        ("ui", "in_progress"),
    ]

    written = await service.write(
        task_id,
        [
            {
                "id": "model",
                "content": "Implement canonical task state",
                "status": "completed",
                "priority": "high",
            },
            {
                "id": "ui",
                "content": "Expose task sessions",
                "status": "completed",
                "priority": "medium",
            },
        ],
    )
    assert [item.status for item in written.todos] == ["completed", "completed"]
    current = await service.read_task(task_id)
    assert [step.status for step in current.plan.steps] == [
        "completed",
        "completed",
    ]


@pytest.mark.asyncio
async def test_ending_execution_session_does_not_freeze_task(
    tmp_path, monkeypatch
):
    _settings, harness, task_id, session_id = await _task_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.task_service
    await service.report_progress(
        task_id,
        summary="Session will end",
    )

    await harness.control.session_coordinator.end_session(session_id)

    after = await service.report_progress(
        task_id,
        summary="Task continues without an active session",
    )
    assert after.progress.summary == "Task continues without an active session"
    assert after.status == "active"
    assert after.session_ids == [session_id]
    record = harness.control.control_state.snapshot_sessions()[session_id]
    assert record.status == "ended"


@pytest.mark.asyncio
async def test_task_survives_control_restart_and_executor_unavailability(
    tmp_path, monkeypatch
):
    settings, harness, task_id, _session_id = await _task_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.task_service
    await service.report_progress(
        task_id,
        summary="Durable before restart",
        next_action="Resume from task state",
    )

    async def offline(_executor_id: str) -> bool:
        return False

    harness.control.executor_transport.is_online = offline  # type: ignore[method-assign]
    await service.report_progress(
        task_id,
        findings=["Executor availability is not task authority"],
    )

    harness.control.control_state.close()
    restarted = build_control_runtime(settings)
    restarted.control_state.start()
    restored = await restarted.task_service.read_task(task_id)
    assert restored.progress.summary == "Durable before restart"
    assert restored.progress.findings == [
        "Executor availability is not task authority"
    ]


@pytest.mark.asyncio
async def test_task_principal_ownership_is_enforced(tmp_path, monkeypatch):
    _settings, harness = _harness(monkeypatch, tmp_path)
    token = bind_oauth_claims({"sub": "alice"})
    try:
        task = await harness.control.task_service.create_task(
            label="alice task"
        )
    finally:
        reset_oauth_claims(token)

    token = bind_oauth_claims({"sub": "bob"})
    try:
        with pytest.raises(PermissionError, match="different principal"):
            await harness.control.task_service.read_task(task.task_id)
    finally:
        reset_oauth_claims(token)

    token = bind_oauth_claims({"sub": "alice"})
    try:
        assert (
            await harness.control.task_service.read_task(task.task_id)
        ).label == "alice task"
    finally:
        reset_oauth_claims(token)


@pytest.mark.asyncio
async def test_session_attachment_enforces_task_principal_ownership(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    token = bind_oauth_claims({"sub": "alice"})
    try:
        task = await harness.control.task_service.create_task(
            label="alice task"
        )
    finally:
        reset_oauth_claims(token)

    token = bind_oauth_claims({"sub": "bob"})
    try:
        with pytest.raises(PermissionError, match="different principal"):
            await harness.control.session_coordinator.start_session(
                workdir="project-a",
                executor_id=harness.executor_id,
                task_id=task.task_id,
            )
    finally:
        reset_oauth_claims(token)

    assert all(
        str(record.task_id or "") != task.task_id
        for record in harness.control.control_state.snapshot_sessions().values()
    )


@pytest.mark.asyncio
async def test_task_history_prunes_oldest_terminal_task_at_bound(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "workgate.control.task_state._TASK_HISTORY_LIMIT_PER_PRINCIPAL", 2
    )
    monkeypatch.setattr(
        "workgate.control.task_state._TASK_TERMINAL_RETENTION_S", 10**9
    )
    service = harness.control.task_service

    first = await service.create_task(label="first")
    await service.cancel_task(first.task_id)
    second = await service.create_task(label="second")
    third = await service.create_task(label="third")

    with pytest.raises(ValueError, match="unknown task_id"):
        await service.read_task(first.task_id)
    assert (await service.read_task(second.task_id)).label == "second"
    assert (await service.read_task(third.task_id)).label == "third"


@pytest.mark.asyncio
async def test_task_retention_never_prunes_terminal_task_with_live_session(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "workgate.control.task_state._TASK_HISTORY_LIMIT_PER_PRINCIPAL", 1
    )
    monkeypatch.setattr(
        "workgate.control.task_state._TASK_TERMINAL_RETENTION_S", 0
    )
    service = harness.control.task_service
    task = await service.create_task(label="still executing")
    started = await harness.control.session_coordinator.start_session(
        workdir="project-a",
        executor_id=harness.executor_id,
        task_id=task.task_id,
    )
    assert isinstance(started, dict)
    session_id = str(started["session_id"])
    await service.cancel_task(task.task_id)

    with pytest.raises(RuntimeError, match="task history limit reached"):
        await service.create_task(label="must not evict live task")
    assert (await service.read_task(task.task_id)).session_ids == [session_id]

    await harness.control.session_coordinator.end_session(session_id)
    replacement = await service.create_task(label="replacement")
    assert replacement.label == "replacement"
    with pytest.raises(ValueError, match="unknown task_id"):
        await service.read_task(task.task_id)


@pytest.mark.asyncio
async def test_concurrent_task_mutations_serialize_against_latest_state(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    task = await harness.control.task_service.create_task()
    service = harness.control.task_service

    results = await asyncio.gather(
        service.report_progress(task.task_id, summary="writer a"),
        service.report_progress(task.task_id, findings=["writer b"]),
    )

    assert len(results) == 2
    current = await service.read_task(task.task_id)
    assert current.progress.summary == "writer a"
    assert current.progress.findings == ["writer b"]


@pytest.mark.asyncio
async def test_task_lifecycle_is_independent(tmp_path, monkeypatch):
    _settings, harness = _harness(monkeypatch, tmp_path)
    service = harness.control.task_service
    task = await service.create_task()

    await service.update_plan(
        task.task_id,
        steps=[
            {"id": "one", "content": "First"},
            {"id": "two", "content": "Second"},
        ],
    )
    with pytest.raises(ValueError, match="unfinished plan steps"):
        await service.finish_task(task.task_id)

    await service.update_plan(
        task.task_id,
        steps=[
            {"id": "one", "content": "First", "status": "completed"},
            {"id": "two", "content": "Second", "status": "skipped"},
        ],
    )
    completed = await service.finish_task(task.task_id)
    assert completed.status == "completed"

    with pytest.raises(ValueError, match="resumed"):
        await service.report_progress(
            task.task_id,
            summary="not yet",
        )

    resumed = await service.resume_task(task.task_id)
    assert resumed.status == "active"
    blocked = await service.block_task(task.task_id)
    assert blocked.status == "blocked"
    resumed_again = await service.resume_task(task.task_id)
    assert resumed_again.status == "active"
    cancelled = await service.cancel_task(task.task_id)
    assert cancelled.status == "cancelled"
    with pytest.raises(ValueError, match="cancelled"):
        await service.resume_task(task.task_id)


@pytest.mark.asyncio
async def test_delete_terminal_task_clears_session_attachments(
    tmp_path, monkeypatch
):
    _settings, harness, task_id, session_id = await _task_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.task_service
    await service.cancel_task(task_id)

    deleted = await service.delete_task(task_id)

    assert deleted.deleted is True
    assert deleted.task_id == task_id
    assert (
        harness.control.control_state.snapshot_sessions()[session_id].task_id
        is None
    )
    with pytest.raises(ValueError, match="unknown task_id"):
        await service.read_task(task_id)


@pytest.mark.asyncio
async def test_task_delete_storage_failure_never_leaves_dangling_session_reference(
    tmp_path, monkeypatch
):
    _settings, harness, task_id, session_id = await _task_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.task_service
    await service.cancel_task(task_id)
    store = harness.control.state_store
    task_path = store.layout.control_task_path(task_id)
    real_remove = store.remove

    def fail_task_remove(path, *args, **kwargs):
        if path == task_path:
            raise OSError("simulated task removal failure")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(store, "remove", fail_task_remove)
    with pytest.raises(OSError, match="task removal failure"):
        await service.delete_task(task_id)

    assert (
        harness.control.control_state.snapshot_sessions()[session_id].task_id
        is None
    )
    retained = await service.read_task(task_id)
    assert retained.status == "cancelled"

    monkeypatch.setattr(store, "remove", real_remove)
    deleted = await service.delete_task(task_id)
    assert deleted.deleted is True


@pytest.mark.asyncio
async def test_task_mutation_audit_records_identity_not_report_contents(
    tmp_path, monkeypatch
):
    settings, harness = _harness(monkeypatch, tmp_path)
    task = await harness.control.task_service.create_task()
    secret_text = "sensitive-progress-body"
    await harness.control.task_service.report_progress(
        task.task_id,
        summary=secret_text,
        blockers=["private blocker detail"],
    )

    records = [
        json.loads(line)
        for line in settings.audit_log_path.read_text(
            encoding="utf-8"
        ).splitlines()
        if line
    ]
    mutations = [row for row in records if row.get("event") == "task_mutation"]
    assert mutations
    latest = mutations[-1]
    assert latest["task"] == task.task_id
    assert latest["operation"] == "report"
    encoded = json.dumps(latest, ensure_ascii=False)
    assert secret_text not in encoded
    assert "private blocker detail" not in encoded


@pytest.mark.asyncio
async def test_task_service_rejects_invalid_inputs(tmp_path, monkeypatch):
    settings, harness = _harness(monkeypatch, tmp_path)
    service = harness.control.task_service
    task = await service.create_task()

    with pytest.raises(ValueError, match="unknown task_id"):
        await service.read_task("task_AAAAAAAAAAAAAAAAAAAAAA")
    with pytest.raises(ValueError, match="id is required"):
        await service.update_plan(
            task.task_id,
            steps=[{"id": "", "content": "content"}],
        )
    with pytest.raises(ValueError, match="at most 50 items"):
        await service.report_progress(
            task.task_id,
            findings=[str(index) for index in range(51)],
        )
    with pytest.raises(ValueError, match="at least one field"):
        await service.report_progress(task.task_id)
    with pytest.raises(ValueError, match="max is"):
        await service.update_plan(
            task.task_id,
            steps=[
                {"id": str(index), "content": "step"}
                for index in range(settings.max_todos + 1)
            ],
        )


@pytest.mark.asyncio
async def test_task_state_enforces_document_byte_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_MAX_TODO_BYTES", "512")
    _settings, harness = _harness(monkeypatch, tmp_path)
    task = await harness.control.task_service.create_task()

    with pytest.raises(ValueError, match="task bytes"):
        await harness.control.task_service.report_progress(
            task.task_id,
            objective="x" * 1_000,
        )


@pytest.mark.asyncio
async def test_task_mutation_survives_audit_append_failure(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    task = await harness.control.task_service.create_task()

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("workgate.control.task_state.audit", fail_audit)
    updated = await harness.control.task_service.report_progress(
        task.task_id,
        summary="canonical write still succeeds",
    )
    assert updated.progress.summary == "canonical write still succeeds"
    restored = await harness.control.task_service.read_task(task.task_id)
    assert restored.progress.summary == "canonical write still succeeds"


@pytest.mark.asyncio
async def test_task_continuation_claim_is_task_scoped_durable_and_single_owner(
    tmp_path, monkeypatch
):
    settings, harness = _harness(monkeypatch, tmp_path)
    service = harness.control.task_service
    task = await service.create_task(
        label="continue me", objective="Finish the semantic task"
    )
    planned = await service.update_plan(
        task.task_id,
        steps=[
            {
                "id": "one",
                "content": "Do the remaining work",
                "status": "in_progress",
            }
        ],
    )
    assert planned.session_ids == []

    clock = [planned.updated_at + 901]
    monkeypatch.setattr(
        "workgate.control.task_state.time.time", lambda: clock[0]
    )

    status = await service.continuation_status(task.task_id)
    assert status["eligible"] is True
    assert status["attempt_count"] == 0
    assert status["max_attempts"] == 10

    first = await service.claim_continuation(task.task_id)
    assert first["claimed"] is True
    assert first["continuation"]["pending"] is True
    claim_id = first["claim_id"]
    assert isinstance(claim_id, str)

    duplicate = await service.claim_continuation(task.task_id)
    assert duplicate["claimed"] is False
    assert duplicate["claim_id"] is None

    validated = await service.validate_continuation(
        task.task_id, claim_id=claim_id
    )
    assert validated["valid"] is True
    assert validated["continuation"]["attempt_count"] == 1
    # Once dispatch is reserved, agent activity can race the host report.
    # Preserve the claim so an accepted host message can still be reported.
    clock[0] += 1
    await service.observe_agent_activity((task.task_id,), observed_at=clock[0])
    reserved = await service.continuation_status(task.task_id)
    assert reserved["pending"] is True

    reported = await service.report_continuation(
        task.task_id,
        claim_id=claim_id,
        accepted=True,
    )
    assert reported["reported"] is True
    assert reported["accepted"] is True
    assert reported["continuation"]["pending"] is False
    assert reported["continuation"]["eligible"] is False

    harness.control.control_state.close()
    restarted = build_control_runtime(settings)
    restarted.control_state.start()
    restored = await restarted.task_service.continuation_status(task.task_id)
    assert restored["attempt_count"] == 1
    assert restored["pending"] is False
    assert (
        await restarted.task_service.read_task(task.task_id)
    ).session_ids == []
    restarted.control_state.close()


@pytest.mark.asyncio
async def test_task_continuation_invalidates_on_task_or_agent_activity(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    service = harness.control.task_service
    task = await service.create_task(label="stale continuation")
    planned = await service.update_plan(
        task.task_id,
        steps=[{"id": "one", "content": "work", "status": "in_progress"}],
    )
    clock = [planned.updated_at + 901]
    monkeypatch.setattr(
        "workgate.control.task_state.time.time", lambda: clock[0]
    )

    claimed = await service.claim_continuation(task.task_id)
    assert claimed["claimed"] is True
    mutation_claim_id = claimed["claim_id"]
    assert isinstance(mutation_claim_id, str)
    clock[0] += 1
    await service.report_progress(task.task_id, summary="new human/task state")
    stale = await service.validate_continuation(
        task.task_id, claim_id=mutation_claim_id
    )
    assert stale["valid"] is False

    clock[0] += 901
    claimed = await service.claim_continuation(task.task_id)
    assert claimed["claimed"] is True
    activity_claim_id = claimed["claim_id"]
    assert isinstance(activity_claim_id, str)
    clock[0] += 1
    await service.observe_agent_activity((task.task_id,), observed_at=clock[0])
    stale = await service.validate_continuation(
        task.task_id, claim_id=activity_claim_id
    )
    assert stale["valid"] is False
    assert stale["continuation"]["pending"] is False


@pytest.mark.asyncio
async def test_task_continuation_backoff_bounds_and_terminal_plan_rules(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    service = harness.control.task_service
    task = await service.create_task(label="bounded continuation")
    planned = await service.update_plan(
        task.task_id,
        steps=[{"id": "one", "content": "work", "status": "in_progress"}],
    )
    clock = [planned.updated_at + 901]
    monkeypatch.setattr(
        "workgate.control.task_state.time.time", lambda: clock[0]
    )

    for attempt in range(10):
        claimed = await service.claim_continuation(task.task_id)
        assert claimed["claimed"] is True
        claim_id = claimed["claim_id"]
        assert isinstance(claim_id, str)
        validated = await service.validate_continuation(
            task.task_id, claim_id=claim_id
        )
        assert validated["valid"] is True
        if attempt == 9:
            revalidated = await service.validate_continuation(
                task.task_id, claim_id=claim_id
            )
            assert revalidated["valid"] is True
            assert revalidated["continuation"]["attempt_count"] == 10
        failed = await service.report_continuation(
            task.task_id,
            claim_id=claim_id,
            accepted=False,
        )
        assert failed["continuation"]["attempt_count"] == attempt + 1
        if attempt < 9:
            assert failed["continuation"]["eligible"] is False
            clock[0] += 301

    exhausted = await service.continuation_status(task.task_id)
    assert exhausted["exhausted"] is True
    assert exhausted["eligible"] is False

    other = await service.create_task(label="finished plan")
    finished = await service.update_plan(
        other.task_id,
        steps=[
            {
                "id": "done",
                "content": "already done",
                "status": "completed",
            }
        ],
    )
    clock[0] = finished.updated_at + 901
    assert (await service.continuation_status(other.task_id))[
        "eligible"
    ] is False

    blocked = await service.create_task(label="blocked plan")
    blocked_plan = await service.update_plan(
        blocked.task_id,
        steps=[{"id": "wait", "content": "wait", "status": "blocked"}],
    )
    await service.block_task(blocked.task_id)
    clock[0] = blocked_plan.updated_at + 901
    assert (await service.continuation_status(blocked.task_id))[
        "eligible"
    ] is False


@pytest.mark.asyncio
async def test_task_continuation_expired_claim_can_be_replaced(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    service = harness.control.task_service
    task = await service.create_task(label="expired claim")
    planned = await service.update_plan(
        task.task_id,
        steps=[{"id": "one", "content": "work", "status": "in_progress"}],
    )
    clock = [planned.updated_at + 901]
    monkeypatch.setattr(
        "workgate.control.task_state.time.time", lambda: clock[0]
    )
    old = await service.claim_continuation(task.task_id)
    assert old["claimed"] is True
    old_claim_id = old["claim_id"]
    assert isinstance(old_claim_id, str)
    clock[0] += 301
    replacement = await service.claim_continuation(task.task_id)
    assert replacement["claimed"] is True
    assert replacement["claim_id"] != old_claim_id

    stale = await service.report_continuation(
        task.task_id,
        claim_id=old_claim_id,
        accepted=True,
    )
    assert stale["reported"] is False
    assert stale["accepted"] is None
    assert stale["continuation"]["pending"] is True


@pytest.mark.asyncio
async def test_idle_policy_blocks_only_after_confirmed_24h(
    tmp_path, monkeypatch
):
    settings, harness = _harness(monkeypatch, tmp_path)
    service = harness.control.task_service
    task = await service.create_task(label="idle")
    assert settings.task_idle_block_after_s == 24 * 60 * 60
    assert await service.sweep_idle_tasks(now=task.updated_at + 86400 - 1) == 0
    assert await service.sweep_idle_tasks(now=task.updated_at + 86401) == 1
    blocked = await service.read_task(task.task_id)
    assert blocked.status == "blocked"
    assert any("Automatically blocked" in x for x in blocked.progress.blockers)
    assert await service.sweep_idle_tasks(now=task.updated_at + 86402) == 0
    resumed = await service.resume_task(task.task_id)
    assert resumed.status == "active"
    assert all(
        "Automatically blocked" not in x for x in resumed.progress.blockers
    )
    assert await service.sweep_idle_tasks(now=resumed.updated_at + 1) == 0


@pytest.mark.asyncio
async def test_idle_policy_observes_agent_heartbeat_and_claim(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    service = harness.control.task_service
    task = await service.create_task(label="active heartbeat")
    await service.observe_agent_activity(
        (task.task_id,), observed_at=task.updated_at + 600
    )
    assert await service.sweep_idle_tasks(now=task.updated_at + 86401) == 0
    assert (
        await service.sweep_idle_tasks(now=task.updated_at + 86400 + 601) == 1
    )
    assert (await service.read_task(task.task_id)).status == "blocked"


@pytest.mark.asyncio
async def test_idle_policy_skips_live_and_unconfirmed_sessions(
    tmp_path, monkeypatch
):
    _settings, harness, task_id, session_id = await _task_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.task_service
    task = await service.read_task(task_id)
    later = task.updated_at + 5 * 86400
    assert await service.sweep_idle_tasks(now=later) == 0

    harness.control.control_state.update_session(
        session_id,
        status="ended",
        executor_absence_confirmed=False,
        updated_at=task.updated_at + 60,
    )
    assert await service.sweep_idle_tasks(now=later) == 0
    assert (await service.read_task(task_id)).status == "active"


@pytest.mark.asyncio
async def test_idle_policy_checks_jobs_after_confirmed_end(
    tmp_path, monkeypatch
):
    _settings, harness, task_id, session_id = await _task_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.task_service
    ended = await harness.control.session_coordinator.end_session(session_id)
    assert ended["ended"] is True
    record = harness.control.control_state.snapshot_sessions()[session_id]
    assert record.executor_absence_confirmed

    # Persisted Session ending is more recent than Task's own updated_at.
    assert (
        await service.sweep_idle_tasks(now=record.updated_at + 86400 - 1) == 0
    )
    service._jobs_may_be_active = lambda sessions: True
    assert await service.sweep_idle_tasks(now=record.updated_at + 86401) == 0
    service._jobs_may_be_active = lambda sessions: False
    assert await service.sweep_idle_tasks(now=record.updated_at + 86401) == 1
    assert (await service.read_task(task_id)).status == "blocked"


@pytest.mark.asyncio
async def test_idle_policy_respects_explicit_disable(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_TASK_IDLE_BLOCK_AFTER_S", "0")
    _settings, harness = _harness(monkeypatch, tmp_path)
    service = harness.control.task_service
    task = await service.create_task()
    assert await service.sweep_idle_tasks(now=task.updated_at + 10 * 86400) == 0
    assert (await service.read_task(task.task_id)).status == "active"


@pytest.mark.asyncio
async def test_idle_policy_uses_real_durable_job_states(tmp_path, monkeypatch):
    from workgate.jobs import managed as managed_jobs
    from workgate.jobs.managed import use_managed_jobs_runtime

    _settings, harness, task_id, session_id = await _task_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.task_service
    await harness.control.session_coordinator.end_session(session_id)
    ended = harness.control.control_state.snapshot_sessions()[session_id]
    later = ended.updated_at + 86401
    with (
        use_managed_jobs_runtime(harness.control.managed_jobs_runtime),
        managed_jobs._store_transaction() as store,
    ):
        store["jobs"].append(
            {
                "job_id": "job_active_copy",
                "kind": "managed",
                "session_id": "another-session",
                "managed_payload": {"dst_session_id": session_id},
                "status": "running",
            }
        )
    assert await service.sweep_idle_tasks(now=later) == 0
    with (
        use_managed_jobs_runtime(harness.control.managed_jobs_runtime),
        managed_jobs._store_transaction() as store,
    ):
        store["jobs"][0]["status"] = "lost"  # Not confirmed terminal.
    assert await service.sweep_idle_tasks(now=later) == 0
    with (
        use_managed_jobs_runtime(harness.control.managed_jobs_runtime),
        managed_jobs._store_transaction() as store,
    ):
        store["jobs"][0]["status"] = "succeeded"
    assert await service.sweep_idle_tasks(now=later) == 1
    assert (await service.read_task(task_id)).status == "blocked"


@pytest.mark.asyncio
async def test_idle_policy_rechecks_heartbeat_under_task_file_lock(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    service = harness.control.task_service
    task = await service.create_task(label="concurrent update")
    now = task.updated_at + 86401
    path = service._path(task.task_id)
    with service._store.transaction(path):
        sweep = asyncio.create_task(service.sweep_idle_tasks(now=now))
        await asyncio.sleep(0.05)
        raw = service._store.read_json(path)
        assert raw is not None
        raw["continuation"] = {"last_agent_activity": now - 1}
        service._store.write_json(path, raw)
    assert await sweep == 0
    assert (await service.read_task(task.task_id)).status == "active"


@pytest.mark.asyncio
async def test_idle_policy_uses_persisted_task_across_control_restart(
    tmp_path, monkeypatch
):
    settings, harness = _harness(monkeypatch, tmp_path)
    original = await harness.control.task_service.create_task(label="restart")
    harness.control.control_state.close()
    restarted = build_control_runtime(settings)
    restarted.control_state.start()
    assert (
        await restarted.task_service.sweep_idle_tasks(
            now=original.updated_at + 86401
        )
        == 1
    )
    assert (
        await restarted.task_service.read_task(original.task_id)
    ).status == "blocked"


@pytest.mark.asyncio
async def test_idle_policy_custom_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_TASK_IDLE_BLOCK_AFTER_S", "3600")
    settings, harness = _harness(monkeypatch, tmp_path)
    assert settings.task_idle_block_after_s == 3600
    task = await harness.control.task_service.create_task()
    assert (
        await harness.control.task_service.sweep_idle_tasks(
            now=task.updated_at + 3599
        )
        == 0
    )
    assert (
        await harness.control.task_service.sweep_idle_tasks(
            now=task.updated_at + 3601
        )
        == 1
    )


@pytest.mark.asyncio
async def test_idle_policy_audit_is_bound_to_own_control_store(
    tmp_path, monkeypatch
):
    from workgate.config.role_config import get_role_config
    from workgate.control import task_state
    from workgate.persistence import (
        FileStateStore,
        get_state_store,
        use_state_store,
    )

    _settings, harness = _harness(monkeypatch, tmp_path)
    service = harness.control.task_service
    task = await service.create_task(label="audit context")
    unrelated = FileStateStore(lambda: tmp_path / "unrelated-state")
    events = []

    def capture(event, **fields):
        events.append((event, fields, get_state_store(), get_role_config()))

    monkeypatch.setattr(task_state, "audit", capture)
    with use_state_store(unrelated):
        assert await service.sweep_idle_tasks(now=task.updated_at + 86401) == 1
        assert get_state_store() is unrelated
    assert [item[0] for item in events] == [
        "task_mutation",
        "task_idle_blocked",
    ]
    assert all(item[2] is harness.control.state_store for item in events)
    assert all(item[3] is harness.control.config for item in events)
    assert events[-1][1]["last_activity_at"] == task.updated_at
    assert events[-1][1]["idle_threshold_s"] == 86400


@pytest.mark.asyncio
async def test_idle_policy_does_not_forget_pruned_session_history(
    tmp_path, monkeypatch
):
    _settings, harness, task_id, session_id = await _task_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.task_service
    await harness.control.session_coordinator.end_session(session_id)
    ending = harness.control.control_state.snapshot_sessions()[session_id]
    assert ending.executor_absence_confirmed
    harness.control.control_state.remove_session(session_id)
    assert (await service.read_task(task_id)).session_ids == []
    assert (
        await service.sweep_idle_tasks(now=ending.updated_at + 3 * 86400) == 0
    )
    assert (await service.read_task(task_id)).status == "active"


@pytest.mark.asyncio
async def test_idle_policy_does_not_guess_legacy_task_history(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    service = harness.control.task_service
    created = await service.create_task(label="legacy")
    path = service._path(created.task_id)
    with service._store.transaction(path):
        raw = service._store.read_json(path)
        assert raw is not None
        raw.pop("session_count")
        service._store.write_json(path, raw)
    assert (
        await service.sweep_idle_tasks(now=created.updated_at + 4 * 86400) == 0
    )
    assert (await service.read_task(created.task_id)).status == "active"


@pytest.mark.asyncio
async def test_idle_policy_requires_all_historical_sessions_retained(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    service = harness.control.task_service
    task = await service.create_task(label="two sessions")
    first = await harness.control.session_coordinator.start_session(
        workdir="project-a",
        executor_id=harness.executor_id,
        task_id=task.task_id,
    )
    second = await harness.control.session_coordinator.start_session(
        workdir="project-b",
        executor_id=harness.executor_id,
        task_id=task.task_id,
    )
    await harness.control.session_coordinator.end_session(first["session_id"])
    await harness.control.session_coordinator.end_session(second["session_id"])
    records = harness.control.control_state.snapshot_sessions()
    now = (
        max(records[x["session_id"]].updated_at for x in (first, second))
        + 86401
    )
    harness.control.control_state.remove_session(first["session_id"])
    assert len((await service.read_task(task.task_id)).session_ids) == 1
    assert await service.sweep_idle_tasks(now=now) == 0
    assert (await service.read_task(task.task_id)).status == "active"
