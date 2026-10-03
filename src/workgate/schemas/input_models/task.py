"""Typed input annotations for durable semantic task tools."""

from typing import Annotated, Any, Literal

from pydantic import Field

TaskIdArg = Annotated[
    str,
    Field(
        min_length=27,
        max_length=128,
        pattern=r"^task_[A-Za-z0-9_-]{22,}$",
        description="Opaque semantic task_id returned by task(action='create').",
    ),
]
OptionalTaskIdArg = Annotated[
    str | None,
    Field(
        default=None,
        max_length=128,
        pattern=r"^task_[A-Za-z0-9_-]{22,}$",
        description="Task id required by every task action except create.",
    ),
]
TaskActionArg = Annotated[
    Literal[
        "create",
        "get",
        "block",
        "resume",
        "report",
        "finish",
        "cancel",
        "delete",
    ],
    Field(description="Semantic task lifecycle action."),
]
TaskLabelArg = Annotated[
    str | None,
    Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Optional human-readable task label.",
    ),
]
TaskObjectiveArg = Annotated[
    str | None,
    Field(
        default=None,
        description="Optional replacement task objective; empty text clears it.",
    ),
]
ProgressSummaryArg = Annotated[
    str | None,
    Field(default=None, description="Optional replacement progress summary."),
]
ProgressFindingsArg = Annotated[
    list[str] | None,
    Field(
        default=None,
        description="Optional replacement findings list; [] clears findings.",
    ),
]
ProgressNextActionArg = Annotated[
    str | None,
    Field(default=None, description="Optional replacement next action."),
]
ProgressBlockersArg = Annotated[
    list[str] | None,
    Field(
        default=None,
        description="Optional replacement blockers list; [] clears blockers.",
    ),
]
PlanStepsArg = Annotated[
    list[dict[str, Any]],
    Field(
        description=(
            "Complete replacement plan. Every step requires an explicit stable id "
            "and may include content, status, and priority; unsupported fields "
            "are rejected."
        ),
    ),
]
