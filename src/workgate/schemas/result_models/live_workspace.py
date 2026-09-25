"""Typed structured outputs for the session-scoped Live Workspace MCP App."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class LiveWorkspaceSession(BaseModel):
    """Canonical Workgate session identity and availability projection."""

    session_id: str
    label: str | None = None
    executor_id: str
    executor_name: str | None = None
    workdir: str
    status: str
    availability: str
    created_at: float
    updated_at: float
    last_active_at: float | None = None


class LiveWorkspaceJob(BaseModel):
    """Bounded job metadata safe for the compact Live Workspace."""

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
    """Allow-listed audit identity fields for recent activity."""

    id: str | None = None
    ts: float | None = None
    event: str | None = None
    tool: str | None = None
    operation: str | None = None
    ok: bool | None = None
    duration_ms: float | None = None


class LiveWorkspaceLinks(BaseModel):
    """Links to fuller authenticated Human UI views."""

    sessions: str
    files: str
    terminals: str
    audit: str


class LiveWorkspaceSnapshot(BaseModel):
    """Server-reconstructed view of one explicit Workgate session."""

    version: Literal[1] = 1
    session: LiveWorkspaceSession
    task: dict[str, Any] | None = None
    compatibility_plan: list[dict[str, Any]] = Field(default_factory=list)
    task_controls_available: bool = False
    task_control_actions: list[
        Literal["block", "resume", "cancel", "next_instruction"]
    ] = Field(default_factory=list)
    task_controls_message: str | None = None
    jobs: list[LiveWorkspaceJob] = Field(default_factory=list)
    jobs_message: str | None = None
    shells: list[LiveWorkspaceShell] = Field(default_factory=list)
    shells_message: str | None = None
    activity: list[LiveWorkspaceActivity] = Field(default_factory=list)
    links: LiveWorkspaceLinks


class LiveWorkspaceEndOutput(BaseModel):
    """Result of an explicitly confirmed Live Workspace session end."""

    session_id: str
    ended: bool
    force_released: bool = False
    stopped_jobs: list[str] = Field(default_factory=list)
    stopped_shells: list[str] = Field(default_factory=list)
