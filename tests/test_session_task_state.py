from __future__ import annotations

import asyncio
import json

import pytest

from tests.helpers import build_paired_control_harness
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.runtime import build_control_runtime
from workgate.control.todos import TodoConflictError


def _configure(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    clear_settings_cache()


async def _harness_with_session(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    (tmp_path / "project").mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    harness = build_paired_control_harness(settings)
    started = await harness.control.session_coordinator.start_session(
        workdir="project",
        label="durable task",
        executor_id=harness.executor_id,
    )
    assert isinstance(started, dict)
    return settings, harness, str(started["session_id"])


@pytest.mark.asyncio
async def test_task_progress_plan_and_todos_share_one_revisioned_document(
    tmp_path, monkeypatch
):
    _settings, harness, session_id = await _harness_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.todo_service

    initial = await service.read_task(session_id)
    assert initial.revision == 0
    assert initial.status == "active"
    assert initial.label == "durable task"
    assert initial.execution_status == "active"
    assert initial.plan.steps == []

    reported = await service.report_progress(
        session_id,
        expected_revision=0,
        objective="Ship issue 134",
        summary="Mapped the current session model",
        findings=["Todo state is already control-owned"],
        next_action="Add a structured plan",
        blockers=[],
    )
    assert reported.revision == 1
    assert reported.objective == "Ship issue 134"
    assert reported.progress.summary == "Mapped the current session model"

    planned = await service.update_plan(
        session_id,
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
                "content": "Expose state in Human UI",
                "status": "in_progress",
                "priority": "medium",
                "note": "Use the same backing document",
            },
        ],
    )
    assert planned.revision == 2
    assert [step.id for step in planned.plan.steps] == ["model", "ui"]

    todos = await service.read(session_id)
    assert todos.revision == 2
    assert [(item.id, item.status) for item in todos.todos] == [
        ("model", "completed"),
        ("ui", "in_progress"),
    ]

    written = await service.write(
        session_id,
        [
            {
                "id": "model",
                "content": "Implement canonical task state",
                "status": "completed",
                "priority": "high",
            },
            {
                "id": "ui",
                "content": "Expose state in Human UI",
                "status": "completed",
                "priority": "medium",
            },
        ],
        expected_revision=2,
    )
    assert written.revision == 3
    current = await service.read_task(session_id)
    assert current.revision == 3
    assert [step.status for step in current.plan.steps] == [
        "completed",
        "completed",
    ]
    assert current.objective == "Ship issue 134"


@pytest.mark.asyncio
async def test_legacy_todo_replacement_preserves_plan_notes(
    tmp_path, monkeypatch
):
    _settings, harness, session_id = await _harness_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.todo_service
    await service.update_plan(
        session_id,
        expected_revision=0,
        steps=[
            {
                "id": "keep-note",
                "content": "Original content",
                "status": "pending",
                "note": "Plan-only context",
            }
        ],
    )

    await service.write(
        session_id,
        [
            {
                "id": "keep-note",
                "content": "Edited through Todo compatibility",
                "status": "in_progress",
                "priority": "medium",
            }
        ],
        expected_revision=1,
    )

    current = await service.read_task(session_id)
    assert current.plan.steps[0].content == "Edited through Todo compatibility"
    assert current.plan.steps[0].status == "in_progress"
    assert current.plan.steps[0].note == "Plan-only context"


@pytest.mark.asyncio
async def test_legacy_todo_write_keeps_preexisting_flexible_status_semantics(
    tmp_path, monkeypatch
):
    _settings, harness, session_id = await _harness_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.todo_service
    long_content = "x" * 20_000

    written = await service.write(
        session_id,
        [
            {
                "content": long_content,
                "status": "waiting_external",
                "priority": "custom",
            }
        ],
        expected_revision=0,
    )

    assert written.revision == 1
    assert written.todos[0].id == "1"
    assert written.todos[0].content == long_content
    assert written.todos[0].status == "waiting_external"
    assert written.todos[0].priority == "custom"
    task = await service.read_task(session_id)
    assert task.plan.steps[0].status == "waiting_external"


@pytest.mark.asyncio
async def test_new_plan_patch_rejects_ambiguous_legacy_duplicate_ids(
    tmp_path, monkeypatch
):
    _settings, harness, session_id = await _harness_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.todo_service

    written = await service.write(
        session_id,
        [
            {"id": "same", "content": "first"},
            {"id": "same", "content": "second"},
        ],
        expected_revision=0,
    )
    assert written.revision == 1

    with pytest.raises(ValueError, match="ambiguous plan step id"):
        await service.update_plan(
            session_id,
            expected_revision=1,
            step_id="same",
            status="completed",
        )

    replaced = await service.update_plan(
        session_id,
        expected_revision=1,
        steps=[
            {"id": "first", "content": "first"},
            {"id": "second", "content": "second"},
        ],
    )
    assert [step.id for step in replaced.plan.steps] == ["first", "second"]


@pytest.mark.asyncio
async def test_task_state_survives_control_restart_and_executor_unavailability(
    tmp_path, monkeypatch
):
    settings, harness, session_id = await _harness_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.todo_service
    await service.report_progress(
        session_id,
        expected_revision=0,
        summary="Durable before restart",
        next_action="Resume from control state",
    )
    await service.update_plan(
        session_id,
        expected_revision=1,
        steps=[{"id": "resume", "content": "Resume durable work"}],
    )

    async def offline(_executor_id: str) -> bool:
        return False

    harness.control.executor_transport.is_online = offline  # type: ignore[method-assign]
    offline_state = await service.read_task(session_id)
    assert offline_state.progress.summary == "Durable before restart"
    offline_update = await service.report_progress(
        session_id,
        expected_revision=2,
        findings=["Executor availability is not task authority"],
    )
    assert offline_update.revision == 3

    harness.control.control_state.close()
    restarted = build_control_runtime(settings)
    restarted.control_state.start()
    restored = await restarted.todo_service.read_task(session_id)
    assert restored.revision == 3
    assert restored.progress.findings == [
        "Executor availability is not task authority"
    ]
    assert restored.plan.steps[0].id == "resume"


@pytest.mark.asyncio
async def test_concurrent_task_mutations_reject_one_stale_revision(
    tmp_path, monkeypatch
):
    _settings, harness, session_id = await _harness_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.todo_service

    results = await asyncio.gather(
        service.report_progress(
            session_id, expected_revision=0, summary="writer a"
        ),
        service.report_progress(
            session_id, expected_revision=0, summary="writer b"
        ),
        return_exceptions=True,
    )

    successes = [
        item for item in results if not isinstance(item, BaseException)
    ]
    conflicts = [
        item for item in results if isinstance(item, TodoConflictError)
    ]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert (await service.read_task(session_id)).revision == 1


@pytest.mark.asyncio
async def test_progress_and_plan_share_one_stale_write_guard(
    tmp_path, monkeypatch
):
    _settings, harness, session_id = await _harness_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.todo_service

    reported = await service.report_progress(
        session_id,
        expected_revision=0,
        summary="progress wins revision zero",
    )
    assert reported.revision == 1

    with pytest.raises(TodoConflictError, match="changed from revision 0 to 1"):
        await service.update_plan(
            session_id,
            expected_revision=0,
            steps=[{"id": "late-plan", "content": "stale plan write"}],
        )

    current = await service.read_task(session_id)
    assert current.revision == 1
    assert current.plan.steps == []
    assert current.progress.summary == "progress wins revision zero"


@pytest.mark.asyncio
async def test_task_terminal_states_and_plan_completion_rules(
    tmp_path, monkeypatch
):
    _settings, harness, session_id = await _harness_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.todo_service

    await service.update_plan(
        session_id,
        expected_revision=0,
        steps=[
            {"id": "one", "content": "First"},
            {"id": "two", "content": "Second"},
        ],
    )
    with pytest.raises(ValueError, match="unfinished plan steps"):
        await service.report_progress(
            session_id, expected_revision=1, task_status="completed"
        )

    await service.update_plan(
        session_id,
        expected_revision=1,
        step_id="one",
        status="completed",
    )
    await service.update_plan(
        session_id,
        expected_revision=2,
        step_id="two",
        status="skipped",
    )
    completed = await service.report_progress(
        session_id,
        expected_revision=3,
        task_status="completed",
    )
    assert completed.status == "completed"
    assert completed.revision == 4

    with pytest.raises(ValueError, match="explicitly resumed"):
        await service.update_plan(
            session_id,
            expected_revision=4,
            step_id="two",
            note="too late",
        )

    resumed = await service.report_progress(
        session_id,
        expected_revision=4,
        task_status="active",
    )
    assert resumed.status == "active"
    cancelled = await service.report_progress(
        session_id,
        expected_revision=5,
        task_status="cancelled",
    )
    assert cancelled.status == "cancelled"
    with pytest.raises(ValueError, match="terminal"):
        await service.report_progress(
            session_id,
            expected_revision=6,
            task_status="active",
        )


@pytest.mark.asyncio
async def test_session_end_retains_read_only_task_history(
    tmp_path, monkeypatch
):
    _settings, harness, session_id = await _harness_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.todo_service
    await service.report_progress(
        session_id,
        expected_revision=0,
        objective="Keep this after executor cleanup",
        summary="Still active as semantic task state",
    )
    await service.update_plan(
        session_id,
        expected_revision=1,
        steps=[{"id": "later", "content": "Continue elsewhere"}],
    )

    await harness.control.session_coordinator.end_session(session_id)

    retained = await service.read_task(session_id)
    assert retained.execution_status == "ended"
    assert retained.status == "active"
    assert retained.objective == "Keep this after executor cleanup"
    assert retained.plan.steps[0].id == "later"
    assert (await service.read(session_id)).revision == 2

    with pytest.raises(ValueError):
        await service.report_progress(
            session_id,
            expected_revision=2,
            summary="must not mutate ended execution sessions",
        )


@pytest.mark.asyncio
async def test_legacy_todos_migrate_into_canonical_task_document_on_write(
    tmp_path, monkeypatch
):
    _settings, harness, session_id = await _harness_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.todo_service
    path = service._path(session_id)
    legacy = {
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
    }
    service._store.write_json(path, legacy)

    projected = await service.read_task(session_id)
    assert projected.revision == 4
    assert projected.plan.steps[0].id == "legacy"
    assert projected.plan.steps[0].status == "in_progress"

    migrated = await service.report_progress(
        session_id,
        expected_revision=4,
        summary="Migrated without a second backing model",
    )
    assert migrated.revision == 5
    stored = service._store.read_json(path)
    assert isinstance(stored, dict)
    assert stored["version"] == 1
    assert "plan" in stored
    assert "todos" not in stored


@pytest.mark.asyncio
async def test_task_mutation_audit_records_metadata_not_report_contents(
    tmp_path, monkeypatch
):
    settings, harness, session_id = await _harness_with_session(
        monkeypatch, tmp_path
    )
    secret_text = "sensitive-progress-body"
    await harness.control.todo_service.report_progress(
        session_id,
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
    mutations = [
        row for row in records if row.get("event") == "session_task_mutation"
    ]
    assert len(mutations) == 1
    assert mutations[0]["session"] == session_id
    assert mutations[0]["operation"] == "progress_report"
    encoded = json.dumps(mutations[0], ensure_ascii=False)
    assert secret_text not in encoded
    assert "private blocker detail" not in encoded


@pytest.mark.asyncio
async def test_task_service_rejects_invalid_progress_and_plan_inputs(
    tmp_path, monkeypatch
):
    settings, harness, session_id = await _harness_with_session(
        monkeypatch, tmp_path
    )
    service = harness.control.todo_service

    with pytest.raises(ValueError, match="unknown session_id"):
        await service.read_task("missing")
    with pytest.raises(ValueError, match="must not be empty"):
        await service.update_plan(
            session_id,
            expected_revision=0,
            step_id="",
            content="content",
        )
    with pytest.raises(ValueError, match="exceeds 256 encoded bytes"):
        await service.update_plan(
            session_id,
            expected_revision=0,
            step_id="x" * 257,
            content="content",
        )
    with pytest.raises(ValueError, match="at most 50 items"):
        await service.report_progress(
            session_id,
            expected_revision=0,
            findings=[str(index) for index in range(51)],
        )

    normalized = await service.report_progress(
        session_id,
        expected_revision=0,
        findings=[" ", "kept"],
    )
    assert normalized.revision == 1
    assert normalized.progress.findings == ["kept"]

    with pytest.raises(ValueError, match="non-negative integer"):
        await service.report_progress(
            session_id,
            expected_revision=True,
            summary="invalid revision",
        )
    with pytest.raises(ValueError, match="at least one field"):
        await service.report_progress(session_id, expected_revision=1)
    with pytest.raises(ValueError, match="unsupported task_status"):
        await service.report_progress(
            session_id,
            expected_revision=1,
            task_status="unknown",
        )
    with pytest.raises(
        ValueError, match="task_status exceeds 64 encoded bytes"
    ):
        await service.report_progress(
            session_id,
            expected_revision=1,
            task_status="x" * 65,
        )
    with pytest.raises(ValueError, match="max is"):
        await service.update_plan(
            session_id,
            expected_revision=1,
            steps=[
                {"id": str(index), "content": "step"}
                for index in range(settings.max_todos + 1)
            ],
        )
    with pytest.raises(ValueError, match=r"steps\[0\] must be a JSON object"):
        await service.update_plan(
            session_id,
            expected_revision=1,
            steps=[None],  # type: ignore[list-item]
        )
    with pytest.raises(ValueError, match="id is required and must be a string"):
        await service.update_plan(
            session_id,
            expected_revision=1,
            steps=[{"content": "missing stable id"}],
        )
    with pytest.raises(ValueError, match="unsupported fields: state"):
        await service.update_plan(
            session_id,
            expected_revision=1,
            steps=[{"id": "one", "content": "one", "state": "completed"}],
        )
    with pytest.raises(ValueError, match="duplicate plan step id"):
        await service.update_plan(
            session_id,
            expected_revision=1,
            steps=[
                {"id": "same", "content": "one"},
                {"id": "same", "content": "two"},
            ],
        )
    with pytest.raises(ValueError, match="unsupported plan step status"):
        await service.update_plan(
            session_id,
            expected_revision=1,
            steps=[{"id": "one", "content": "one", "status": "unknown"}],
        )
    with pytest.raises(ValueError, match="either steps"):
        await service.update_plan(
            session_id,
            expected_revision=1,
            steps=[],
            step_id="one",
            content="one",
        )
    with pytest.raises(ValueError, match="requires steps or step_id"):
        await service.update_plan(session_id, expected_revision=1)
    with pytest.raises(ValueError, match="step_id is required"):
        await service.update_plan(
            session_id,
            expected_revision=1,
            content="one",
        )
    with pytest.raises(ValueError, match="step_id update requires"):
        await service.update_plan(
            session_id,
            expected_revision=1,
            step_id="one",
        )
    with pytest.raises(ValueError, match="unsupported plan step status"):
        await service.update_plan(
            session_id,
            expected_revision=1,
            step_id="one",
            status="unknown",
        )
    with pytest.raises(ValueError, match="status exceeds 64 encoded bytes"):
        await service.update_plan(
            session_id,
            expected_revision=1,
            step_id="one",
            status="x" * 65,
        )

    created = await service.update_plan(
        session_id,
        expected_revision=1,
        steps=[{"id": "one", "content": "initial"}],
    )
    assert created.revision == 2
    patched = await service.update_plan(
        session_id,
        expected_revision=2,
        step_id="one",
        content="updated",
        priority="high",
        note="context",
    )
    assert patched.revision == 3
    assert patched.plan.steps[0].content == "updated"
    assert patched.plan.steps[0].priority == "high"
    assert patched.plan.steps[0].note == "context"
    with pytest.raises(ValueError, match="unknown plan step id"):
        await service.update_plan(
            session_id,
            expected_revision=3,
            step_id="missing",
            status="completed",
        )


@pytest.mark.asyncio
async def test_task_state_enforces_document_byte_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_MAX_TODO_BYTES", "512")
    _settings, harness, session_id = await _harness_with_session(
        monkeypatch, tmp_path
    )

    with pytest.raises(ValueError, match="session-task bytes"):
        await harness.control.todo_service.report_progress(
            session_id,
            expected_revision=0,
            objective="x" * 1_000,
        )


@pytest.mark.asyncio
async def test_task_mutation_survives_audit_append_failure(
    tmp_path, monkeypatch
):
    _settings, harness, session_id = await _harness_with_session(
        monkeypatch, tmp_path
    )

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("workgate.control.todos.audit", fail_audit)
    updated = await harness.control.todo_service.report_progress(
        session_id,
        expected_revision=0,
        summary="canonical write still succeeds",
    )
    assert updated.revision == 1
    restored = await harness.control.todo_service.read_task(session_id)
    assert restored.progress.summary == "canonical write still succeeds"
