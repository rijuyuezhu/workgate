"""Durable semantic task MCP tool registry."""

from ...schemas.input_models.task import (
    OptionalTaskIdArg,
    PlanStepsArg,
    ProgressBlockersArg,
    ProgressFindingsArg,
    ProgressNextActionArg,
    ProgressSummaryArg,
    TaskActionArg,
    TaskIdArg,
    TaskLabelArg,
    TaskObjectiveArg,
)
from ...schemas.result_models.task import TaskDeleteOutput, TaskOutput
from ..declarative import DeclarativeToolRegistry


class TaskToolRegistry(DeclarativeToolRegistry):
    """Register semantic task lifecycle and plan tools."""

    name = "task"


task_tool = TaskToolRegistry.get_tool_decorator()


@task_tool(
    http_method="POST",
    http_path="/tools/task",
    oauth_scopes=("shell:read", "shell:write"),
    timeout_cancellable=False,
)
async def task(
    action: TaskActionArg,
    task_id: OptionalTaskIdArg = None,
    label: TaskLabelArg = None,
    objective: TaskObjectiveArg = None,
    summary: ProgressSummaryArg = None,
    findings: ProgressFindingsArg = None,
    next_action: ProgressNextActionArg = None,
    blockers: ProgressBlockersArg = None,
) -> TaskOutput | TaskDeleteOutput:
    """Create, inspect, report, block, resume, finish, cancel, or delete one semantic task. task_id is independent of executor/workdir identity."""
    del (
        action,
        task_id,
        label,
        objective,
        summary,
        findings,
        next_action,
        blockers,
    )
    raise RuntimeError("task requires control routing")


@task_tool(
    http_method="POST",
    http_path="/tools/task-plan",
    oauth_scopes=("shell:read", "shell:write"),
    timeout_cancellable=False,
)
async def task_plan(
    task_id: TaskIdArg,
    steps: PlanStepsArg,
) -> TaskOutput:
    """Replace one semantic task's complete plan. This never chooses or changes an execution session."""
    del task_id, steps
    raise RuntimeError("task_plan requires control routing")
