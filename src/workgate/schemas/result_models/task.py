"""Typed structured outputs for durable semantic task state."""

from typing import Literal

from pydantic import BaseModel, Field

TaskStatus = Literal["active", "blocked", "completed", "cancelled"]


class TaskProgress(BaseModel):
    """Latest durable progress handoff for one semantic task."""

    summary: str | None = Field(
        default=None, description="Current concise progress summary."
    )
    findings: list[str] = Field(
        default_factory=list,
        description="Current durable findings worth carrying across turns.",
    )
    next_action: str | None = Field(
        default=None, description="Recommended next concrete action."
    )
    blockers: list[str] = Field(
        default_factory=list,
        description="Current blockers preventing or constraining progress.",
    )


class TaskPlanStep(BaseModel):
    """One stable step in a semantic task plan."""

    id: str = Field(description="Stable caller-visible plan step identifier.")
    content: str = Field(description="Human-readable plan step text.")
    status: str = Field(
        default="pending",
        description=(
            "Plan step status: pending, in_progress, completed, skipped, or blocked."
        ),
    )
    priority: str = Field(
        default="medium",
        description="Compatibility priority label retained from Todo state.",
    )


class TaskPlan(BaseModel):
    """Structured machine-readable plan for one semantic task."""

    steps: list[TaskPlanStep] = Field(
        default_factory=list, description="Ordered plan steps with stable IDs."
    )


class TaskDocument(BaseModel):
    """Canonical revisioned semantic task document."""

    version: Literal[2] = 2
    revision: int = Field(
        default=0,
        ge=0,
        description="Monotonic document revision used for optimistic concurrency.",
    )
    created_at: float = Field(
        description="Unix timestamp when the task was created."
    )
    updated_at: float = Field(
        description="Unix timestamp of the latest task mutation."
    )
    label: str | None = Field(
        default=None, description="Optional human-readable task label."
    )
    objective: str | None = Field(
        default=None, description="Optional durable task objective."
    )
    status: TaskStatus = Field(
        default="active", description="Semantic task lifecycle status."
    )
    progress: TaskProgress = Field(default_factory=TaskProgress)
    plan: TaskPlan = Field(default_factory=TaskPlan)


class TaskOutput(TaskDocument):
    """Canonical task state plus its execution-session attachments."""

    task_id: str = Field(description="Opaque durable semantic task identifier.")
    session_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Execution sessions explicitly attached to this task, including retained "
            "ended-session history while those session records exist."
        ),
    )


class TaskDeleteOutput(BaseModel):
    """Result of deleting one terminal semantic task."""

    task_id: str
    deleted: Literal[True] = True
