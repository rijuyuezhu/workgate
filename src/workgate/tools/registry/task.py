"""Durable shared-session task-state MCP tool registry."""

from ...schemas.input_models.session import SessionIdArg
from ...schemas.input_models.task import (
    ExpectedTaskRevisionArg,
    PlanStepsArg,
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
    """Read control-owned task progress and plan state for one explicit Workgate session. Ended sessions remain readable."""
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
    """Update durable task progress for one active session using expected_revision. Completing requires every plan step to be completed or skipped."""
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
    steps: PlanStepsArg,
) -> SessionTaskOutput:
    """Replace the structured plan for one active session using expected_revision. Every step requires a stable ID; Todo APIs expose the same plan."""
    del session_id, expected_revision, steps
    raise RuntimeError("update_session_plan requires control routing")
