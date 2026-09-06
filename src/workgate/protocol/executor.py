"""Executor protocol v1 wire models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .ids import CommandId, SessionId

EXECUTOR_PROTOCOL_VERSION = 1
EXECUTOR_PAIR_START_PATH = "/executor/v1/pair/start"
EXECUTOR_PAIR_POLL_PATH = "/executor/v1/pair/poll"
EXECUTOR_HELLO_PATH = "/executor/v1/hello"
EXECUTOR_HEARTBEAT_PATH = "/executor/v1/heartbeat"
EXECUTOR_POLL_PATH = "/executor/v1/poll"
EXECUTOR_RESULT_PATH = "/executor/v1/result"


class ExecutorRuntimeSummary(BaseModel):
    """Bounded runtime/build metadata reported during hello."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workgate_version: str = Field(min_length=1, max_length=128)
    build: str | None = Field(default=None, min_length=1, max_length=128)
    platform: str | None = Field(default=None, min_length=1, max_length=128)


class SessionInventorySummary(BaseModel):
    """Thin authoritative session row in the complete hello inventory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: SessionId
    resolved_workdir: str = Field(min_length=1, max_length=4096)


class ShellInventorySummary(BaseModel):
    """Thin persistent-shell ownership row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    shell_id: str = Field(min_length=1, max_length=128)
    session_id: SessionId


class JobInventorySummary(BaseModel):
    """Thin background-job ownership/status row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(min_length=1, max_length=128)
    session_id: SessionId
    status: str = Field(min_length=1, max_length=64)


class ExecutorHelloRequest(BaseModel):
    """Authenticated reconnect hello with a complete bounded inventory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = 1
    runtime: ExecutorRuntimeSummary
    capabilities: tuple[str, ...] = ()
    boot_id: str | None = Field(default=None, min_length=1, max_length=256)
    workspace_root: str | None = Field(
        default=None, min_length=1, max_length=4096
    )
    sessions: tuple[SessionInventorySummary, ...]
    shells: tuple[ShellInventorySummary, ...]
    jobs: tuple[JobInventorySummary, ...]


class ExecutorHelloResponse(BaseModel):
    """Control timing policy returned after a successful authenticated hello."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = 1
    heartbeat_interval_s: int = Field(gt=0)
    offline_after_s: int = Field(gt=0)
    poll_timeout_s: int = Field(gt=0)

    @model_validator(mode="after")
    def _offline_threshold_exceeds_heartbeat(self) -> ExecutorHelloResponse:
        if self.offline_after_s <= self.heartbeat_interval_s:
            raise ValueError("offline_after_s must exceed heartbeat_interval_s")
        return self


class ExecutorHeartbeatRequest(BaseModel):
    """Empty authenticated presence request; the endpoint may reply with 204."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutorPollRequest(BaseModel):
    """Empty authenticated long-poll request for at most one command."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutorCommand(BaseModel):
    """One ordinary executor operation offered exactly once by control."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: CommandId
    op: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    session_id: SessionId | None = None
    args: dict[str, JsonValue] = Field(default_factory=dict)


class OperationError(BaseModel):
    """Feature-owned operation failure returned inside an executor result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    message: str = Field(min_length=1, max_length=1000)


class ExecutorResult(BaseModel):
    """One success or feature-specific failure for the same live command id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: CommandId
    ok: bool
    result: JsonValue = None
    error: OperationError | None = None

    @model_validator(mode="after")
    def _match_result_shape(self) -> ExecutorResult:
        if self.ok and self.error is not None:
            raise ValueError(
                "successful executor result must not include error"
            )
        if not self.ok and self.error is None:
            raise ValueError("failed executor result must include error")
        if not self.ok and self.result is not None:
            raise ValueError("failed executor result must not include result")
        return self
