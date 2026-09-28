"""Typed input annotations for durable session task tools."""

from typing import Annotated, Any

from pydantic import Field

ExpectedTaskRevisionArg = Annotated[
    int,
    Field(
        ge=0,
        strict=True,
        description="Current task revision that must still match before mutation.",
    ),
]

TaskObjectiveArg = Annotated[
    str | None,
    Field(
        default=None,
        description="Optional replacement task objective; empty text clears it.",
    ),
]

TaskStatusArg = Annotated[
    str | None,
    Field(
        default=None,
        description="Optional task status: active, blocked, completed, or cancelled.",
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
