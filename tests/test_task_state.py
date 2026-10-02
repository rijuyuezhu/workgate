import asyncio
import json

import pytest

from tests.helpers import build_paired_control_harness
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.runtime import build_control_runtime
from workgate.control.task_state import TaskRevisionConflictError
from workgate.oauth.core.context import bind_oauth_claims, reset_oauth_claims


def _configure(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
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
    assert created.revision == 0
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
async def test_session_attachment_cannot_be_rebound(tmp_path, monkeypatch):
    _settings, harness, task_id, session_id = await _task_with_session(
        monkeypatch, tmp_path
    )
    other = await harness.control.task_service.create_task(label="other")

    with pytest.raises(ValueError, match="attachment cannot change"):
        harness.control.control_state.attach_session_task(
            session_id, other.task_id
        )

    assert (
        str(
            harness.control.control_state.snapshot_sessions()[
                session_id
            ].task_id
        )
        == task_id
    )


@pytest.mark.asyncio
async def test_task_progress_plan_and_todos_share_one_revisioned_document(
    tmp_path, monkeypatch
):
    _settings, harness, task_id, _session_id = await _task_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.task_service

    initial = await service.read_task(task_id)
    assert initial.revision == 0
    assert initial.plan.steps == []

    reported = await service.report_progress(
        task_id,
        expected_revision=0,
        summary="Mapped the execution boundary",
        findings=["Task state is control-owned"],
        next_action="Add a structured plan",
        blockers=[],
    )
    assert reported.revision == 1
    assert reported.progress.summary == "Mapped the execution boundary"

    planned = await service.update_plan(
        task_id,
        expected_revision=1,
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
    assert planned.revision == 2

    todos = await service.read(task_id)
    assert todos.revision == 2
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
        expected_revision=2,
    )
    assert written.revision == 3
    current = await service.read_task(task_id)
    assert current.revision == 3
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
    before = await service.report_progress(
        task_id,
        expected_revision=0,
        summary="Session will end",
    )

    await harness.control.session_coordinator.end_session(session_id)

    after = await service.report_progress(
        task_id,
        expected_revision=before.revision,
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
        expected_revision=0,
        summary="Durable before restart",
        next_action="Resume from task state",
    )

    async def offline(_executor_id: str) -> bool:
        return False

    harness.control.executor_transport.is_online = offline  # type: ignore[method-assign]
    offline_update = await service.report_progress(
        task_id,
        expected_revision=1,
        findings=["Executor availability is not task authority"],
    )
    assert offline_update.revision == 2

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
    await service.cancel_task(first.task_id, expected_revision=0)
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
    await service.cancel_task(task.task_id, expected_revision=0)

    with pytest.raises(RuntimeError, match="task history limit reached"):
        await service.create_task(label="must not evict live task")
    assert (await service.read_task(task.task_id)).session_ids == [session_id]

    await harness.control.session_coordinator.end_session(session_id)
    replacement = await service.create_task(label="replacement")
    assert replacement.label == "replacement"
    with pytest.raises(ValueError, match="unknown task_id"):
        await service.read_task(task.task_id)


@pytest.mark.asyncio
async def test_concurrent_task_mutations_reject_stale_revision(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    task = await harness.control.task_service.create_task()
    service = harness.control.task_service

    results = await asyncio.gather(
        service.report_progress(
            task.task_id, expected_revision=0, summary="writer a"
        ),
        service.report_progress(
            task.task_id, expected_revision=0, summary="writer b"
        ),
        return_exceptions=True,
    )

    assert (
        len([item for item in results if not isinstance(item, BaseException)])
        == 1
    )
    assert (
        len(
            [
                item
                for item in results
                if isinstance(item, TaskRevisionConflictError)
            ]
        )
        == 1
    )
    assert (await service.read_task(task.task_id)).revision == 1


@pytest.mark.asyncio
async def test_task_lifecycle_is_independent_and_revision_guarded(
    tmp_path, monkeypatch
):
    _settings, harness = _harness(monkeypatch, tmp_path)
    service = harness.control.task_service
    task = await service.create_task()

    await service.update_plan(
        task.task_id,
        expected_revision=0,
        steps=[
            {"id": "one", "content": "First"},
            {"id": "two", "content": "Second"},
        ],
    )
    with pytest.raises(ValueError, match="unfinished plan steps"):
        await service.finish_task(task.task_id, expected_revision=1)

    await service.update_plan(
        task.task_id,
        expected_revision=1,
        steps=[
            {"id": "one", "content": "First", "status": "completed"},
            {"id": "two", "content": "Second", "status": "skipped"},
        ],
    )
    completed = await service.finish_task(task.task_id, expected_revision=2)
    assert completed.status == "completed"
    assert completed.revision == 3

    with pytest.raises(ValueError, match="resumed"):
        await service.report_progress(
            task.task_id,
            expected_revision=3,
            summary="not yet",
        )

    resumed = await service.resume_task(task.task_id, expected_revision=3)
    assert resumed.status == "active"
    blocked = await service.block_task(task.task_id, expected_revision=4)
    assert blocked.status == "blocked"
    resumed_again = await service.resume_task(task.task_id, expected_revision=5)
    assert resumed_again.status == "active"
    cancelled = await service.cancel_task(task.task_id, expected_revision=6)
    assert cancelled.status == "cancelled"
    with pytest.raises(ValueError, match="cancelled"):
        await service.resume_task(task.task_id, expected_revision=7)


@pytest.mark.asyncio
async def test_delete_terminal_task_clears_session_attachments(
    tmp_path, monkeypatch
):
    _settings, harness, task_id, session_id = await _task_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.task_service
    cancelled = await service.cancel_task(task_id, expected_revision=0)

    deleted = await service.delete_task(
        task_id, expected_revision=cancelled.revision
    )

    assert deleted.deleted is True
    assert deleted.task_id == task_id
    assert (
        harness.control.control_state.snapshot_sessions()[session_id].task_id
        is None
    )
    with pytest.raises(ValueError, match="unknown task_id"):
        await service.read_task(task_id)


@pytest.mark.asyncio
async def test_legacy_session_task_migrates_to_deterministic_task_id(
    tmp_path, monkeypatch
):
    settings, harness = _harness(monkeypatch, tmp_path)
    started = await harness.control.session_coordinator.start_session(
        workdir="project-a",
        label="legacy task",
        executor_id=harness.executor_id,
    )
    assert isinstance(started, dict)
    session_id = str(started["session_id"])
    legacy_path = harness.control.state_store.layout.control_task_state_path(
        session_id
    )
    harness.control.state_store.write_json(
        legacy_path,
        {
            "revision": 4,
            "updated_at": 123.0,
            "todos": [
                {
                    "id": "legacy",
                    "content": "Existing todo",
                    "status": "in_progress",
                    "priority": "high",
                }
            ],
        },
    )

    migrated = await harness.control.task_service.migrate_legacy_sessions()
    assert migrated == 1
    record = harness.control.control_state.snapshot_sessions()[session_id]
    assert record.task_id is not None
    task_id = str(record.task_id)
    assert task_id == harness.control.task_service._legacy_task_id(session_id)
    assert not legacy_path.exists()

    task = await harness.control.task_service.read_task(task_id)
    assert task.revision == 4
    assert task.label == "legacy task"
    assert task.plan.steps[0].id == "legacy"
    assert task.session_ids == [session_id]

    harness.control.control_state.close()
    restarted = build_control_runtime(settings)
    restarted.control_state.start()
    restored = await restarted.task_service.read_task(task_id)
    assert restored.plan.steps[0].status == "in_progress"


@pytest.mark.asyncio
async def test_task_mutation_audit_records_identity_not_report_contents(
    tmp_path, monkeypatch
):
    settings, harness = _harness(monkeypatch, tmp_path)
    task = await harness.control.task_service.create_task()
    secret_text = "sensitive-progress-body"
    await harness.control.task_service.report_progress(
        task.task_id,
        expected_revision=0,
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
            expected_revision=0,
            steps=[{"id": "", "content": "content"}],
        )
    with pytest.raises(ValueError, match="at most 50 items"):
        await service.report_progress(
            task.task_id,
            expected_revision=0,
            findings=[str(index) for index in range(51)],
        )
    with pytest.raises(ValueError, match="at least one field"):
        await service.report_progress(task.task_id, expected_revision=0)
    with pytest.raises(ValueError, match="max is"):
        await service.update_plan(
            task.task_id,
            expected_revision=0,
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
            expected_revision=0,
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
        expected_revision=0,
        summary="canonical write still succeeds",
    )
    assert updated.revision == 1
    restored = await harness.control.task_service.read_task(task.task_id)
    assert restored.progress.summary == "canonical write still succeeds"
