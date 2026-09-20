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
    list[dict[str, Any]] | None,
    Field(
        default=None,
        description=(
            "Optional complete replacement plan. Every step requires an explicit "
            "stable id and may include content, status, priority, and note; "
            "unsupported fields are rejected."
        ),
    ),
]

PlanStepIdArg = Annotated[
    str | None,
    Field(default=None, description="Stable plan step ID to update in place."),
]

PlanStepStatusArg = Annotated[
    str | None,
    Field(
        default=None,
        description=(
            "Optional replacement step status: pending, in_progress, completed, "
            "skipped, or blocked."
        ),
    ),
]

PlanStepContentArg = Annotated[
    str | None,
    Field(default=None, description="Optional replacement plan step content."),
]

PlanStepPriorityArg = Annotated[
    str | None,
    Field(
        default=None, description="Optional replacement compatibility priority."
    ),
]

PlanStepNoteArg = Annotated[
    str | None,
    Field(
        default=None,
        description="Optional replacement note; empty text clears the note.",
    ),
]
