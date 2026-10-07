"""Typed structured outputs for the task-centric Live Workspace MCP App."""

from typing import Literal

from pydantic import BaseModel, Field

from .task import TaskOutput


class LiveWorkspaceSession(BaseModel):
    """One execution session attached to the displayed semantic task."""

    session_id: str
    label: str | None = None
    executor_id: str
    executor_name: str | None = None
    workdir: str | None = None
    status: str
    availability: str
    created_at: float
    updated_at: float
    last_active_at: float | None = None


class LiveWorkspaceJob(BaseModel):
    """Bounded job metadata for one explicitly selected execution session."""

    job_id: str
    kind: str
    name: str
    status: str
    cwd: str
    created_at: float
    updated_at: float
    completed_at: float | None = None
    attempts: int


class LiveWorkspaceShell(BaseModel):
    """Bounded persistent-shell metadata without command or terminal output."""

    shell_id: str
    name: str | None = None
    cwd: str | None = None
    backend: str | None = None


class LiveWorkspaceActivity(BaseModel):
    """Allow-listed task-attributed audit identity fields."""

    id: str | None = None
    ts: float | None = None
    event: str | None = None
    tool: str | None = None
    operation: str | None = None
    session: str | None = None
    ok: bool | None = None
    duration_ms: float | None = None


class LiveWorkspaceLinks(BaseModel):
    """Links to fuller authenticated Human UI views for a selected task/session."""

    tasks: str
    files: str
    terminals: str
    audit: str


class LiveWorkspaceContinuation(BaseModel):
    """Bounded automatic-continuation state for the displayed task."""

    eligible: bool
    pending: bool
    pending_expires_at: float | None = None
    attempt_count: int
    max_attempts: int
    due_at: float
    exhausted: bool


class LiveWorkspaceContinuationResult(BaseModel):
    """Result of one private Live Workspace continuation handshake step."""

    action: Literal["claim", "validate", "report"]
    task: TaskOutput
    continuation: LiveWorkspaceContinuation
    claim_id: str | None = None
    claimed: bool | None = None
    valid: bool | None = None
    reported: bool | None = None
    accepted: bool | None = None


class LiveWorkspaceSnapshot(BaseModel):
    """Task view with its execution-session attachments."""

    version: Literal[2] = 2
    task: TaskOutput
    sessions: list[LiveWorkspaceSession] = Field(default_factory=list)
    session: LiveWorkspaceSession | None = Field(
        default=None,
        description="Explicitly selected execution session, if any.",
    )
    task_control_actions: list[
        Literal["block", "resume", "cancel", "next_instruction"]
    ] = Field(default_factory=list)
    task_controls_message: str | None = None
    continuation: LiveWorkspaceContinuation
    jobs: list[LiveWorkspaceJob] = Field(default_factory=list)
    jobs_message: str | None = None
    shells: list[LiveWorkspaceShell] = Field(default_factory=list)
    shells_message: str | None = None
    activity: list[LiveWorkspaceActivity] = Field(default_factory=list)
    links: LiveWorkspaceLinks | None = None
