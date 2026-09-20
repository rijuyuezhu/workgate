"""Durable shared-session task-state MCP tool registry."""

from ...schemas.input_models.session import SessionIdArg
from ...schemas.input_models.task import (
    ExpectedTaskRevisionArg,
    PlanStepContentArg,
    PlanStepIdArg,
    PlanStepNoteArg,
    PlanStepPriorityArg,
    PlanStepsArg,
    PlanStepStatusArg,
    ProgressBlockersArg,
    ProgressFindingsArg,
    ProgressNextActionArg,
    ProgressSummaryArg,
    TaskObjectiveArg,
    TaskStatusArg,
)
from ...schemas.result_models.task import SessionTaskOutput
from ..declarative import DeclarativeToolRegistry


class TaskToolRegistry(DeclarativeToolRegistry):
    """Register durable session task/progress/plan tools."""

    name = "task"


task_tool = TaskToolRegistry.get_tool_decorator()


@task_tool(
    http_method="GET",
    http_path="/tools/session-task",
    annotations="read_only",
    oauth_scopes=("shell:read",),
)
async def read_session_task(session_id: SessionIdArg) -> SessionTaskOutput:
    """Read the durable task objective, semantic progress, and structured plan associated with one explicit Workgate session. The task state is control-owned and remains readable after session_end; execution_status is separate and an ended session cannot be revived through this API."""
    del session_id
    raise RuntimeError("read_session_task requires control routing")


@task_tool(
    http_method="POST",
    http_path="/tools/session-progress",
    oauth_scopes=("shell:read", "shell:write"),
    timeout_cancellable=False,
)
async def report_session_progress(
    session_id: SessionIdArg,
    expected_revision: ExpectedTaskRevisionArg,
    objective: TaskObjectiveArg = None,
    summary: ProgressSummaryArg = None,
    findings: ProgressFindingsArg = None,
    next_action: ProgressNextActionArg = None,
    blockers: ProgressBlockersArg = None,
    task_status: TaskStatusArg = None,
) -> SessionTaskOutput:
    """Revision-guardedly update durable semantic progress for one explicit active Workgate session. Supply at least one field. task_status is semantic task state, not executor-session state; cancelled tasks are terminal, completed tasks can only be changed by explicitly reporting task_status=active first. Completing a task requires every existing plan step to be completed or skipped."""
    del (
        session_id,
        expected_revision,
        objective,
        summary,
        findings,
        next_action,
        blockers,
        task_status,
    )
    raise RuntimeError("report_session_progress requires control routing")


@task_tool(
    http_method="POST",
    http_path="/tools/session-plan",
    oauth_scopes=("shell:read", "shell:write"),
    timeout_cancellable=False,
)
async def update_session_plan(
    session_id: SessionIdArg,
    expected_revision: ExpectedTaskRevisionArg,
    steps: PlanStepsArg = None,
    step_id: PlanStepIdArg = None,
    status: PlanStepStatusArg = None,
    content: PlanStepContentArg = None,
    priority: PlanStepPriorityArg = None,
    note: PlanStepNoteArg = None,
) -> SessionTaskOutput:
    """Revision-guardedly replace a whole structured plan or update one stable step on an explicit active Workgate session. Replacement steps require explicit stable IDs and accept only id/content/status/priority/note; unsupported fields are rejected instead of silently ignored. Pass steps for a complete replacement, or step_id plus one or more fields for an in-place step update. The canonical plan and legacy Todo compatibility APIs share one backing document and one revision."""
    del (
        session_id,
        expected_revision,
        steps,
        step_id,
        status,
        content,
        priority,
        note,
    )
    raise RuntimeError("update_session_plan requires control routing")
