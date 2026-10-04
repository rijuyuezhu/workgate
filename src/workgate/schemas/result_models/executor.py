"""Structured executor fleet discovery and administration outputs."""

from typing import Literal

from pydantic import BaseModel, Field

from ...protocol.executor import ExecutorRuntimeSummary


class ExecutorSessionCapacity(BaseModel):
    """Global execution-session capacity visible to fleet admission."""

    used: int = Field(ge=0)
    limit: int | None = Field(default=None, ge=1)
    available: bool


class ExecutorFleetEntry(BaseModel):
    """One compact executor discovery row."""

    executor_id: str
    name: str
    created_at: float
    trusted: bool
    revoked_at: float | None = None
    draining: bool = False
    online: bool
    last_seen_at: float | None = None
    runtime: ExecutorRuntimeSummary | None = None
    required_workgate_version: str
    runtime_update_required: bool
    capabilities: list[str] = Field(default_factory=list)
    active_sessions: int = Field(default=0, ge=0)
    queued_commands: int = Field(default=0, ge=0)
    offered_commands: int = Field(default=0, ge=0)
    command_limit: int = Field(ge=1)
    session_admission: bool
    session_admission_reasons: list[str] = Field(default_factory=list)


class ExecutorBootstrapRecipe(BaseModel):
    """Standard provider-neutral enrollment path."""

    url: str
    command: str
    pairing_approval: Literal["human_required"] = "human_required"


class ExecutorFleetOutput(BaseModel):
    """Compact result for executor discovery/admin actions."""

    action: str
    executors: list[ExecutorFleetEntry] = Field(default_factory=list)
    executor: ExecutorFleetEntry | None = None
    session_capacity: ExecutorSessionCapacity | None = None
    bootstrap: ExecutorBootstrapRecipe | None = None
    cancelled_queued: int | None = Field(default=None, ge=0)
    preserved_offered: int | None = Field(default=None, ge=0)
