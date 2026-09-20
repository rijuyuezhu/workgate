"""Typed structured outputs for durable shared-session task state."""

from typing import Literal

from pydantic import BaseModel, Field

TaskStatus = Literal["active", "blocked", "completed", "cancelled"]


class SessionProgress(BaseModel):
    """Latest semantic progress handoff for one session task."""

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
    updated_at: float | None = Field(
        default=None,
        description="Unix timestamp when semantic progress was last reported.",
    )


class SessionPlanStep(BaseModel):
    """One stable step in the session task plan."""

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
    note: str | None = Field(
        default=None, description="Optional bounded note for this step."
    )


class SessionPlan(BaseModel):
    """Structured machine-readable plan for one session task."""

    steps: list[SessionPlanStep] = Field(
        default_factory=list, description="Ordered plan steps with stable IDs."
    )


class SessionTaskDocument(BaseModel):
    """Canonical persisted task state associated with one Workgate session."""

    version: Literal[1] = 1
    revision: int = Field(
        default=0,
        ge=0,
        description="Monotonic document revision used for optimistic concurrency.",
    )
    updated_at: float | None = Field(
        default=None, description="Unix timestamp of the latest task mutation."
    )
    objective: str | None = Field(
        default=None, description="Optional durable task objective."
    )
    status: TaskStatus = Field(
        default="active", description="Semantic task lifecycle status."
    )
    progress: SessionProgress = Field(default_factory=SessionProgress)
    plan: SessionPlan = Field(default_factory=SessionPlan)


class SessionTaskOutput(SessionTaskDocument):
    """Canonical task state plus its existing execution-session projection."""

    session_id: str = Field(description="Explicit Workgate session identifier.")
    label: str | None = Field(
        default=None,
        description="Existing canonical human-readable session label.",
    )
    execution_status: str = Field(
        description="Execution-session lifecycle status, separate from task status."
    )
