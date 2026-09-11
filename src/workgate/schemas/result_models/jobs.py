"""Typed structured outputs for tracked shell and managed job tools."""

from typing import Literal

from pydantic import BaseModel, Field

type JobStatus = Literal[
    "starting",
    "running",
    "stopping",
    "retrying",
    "succeeded",
    "failed",
    "exited",
    "stopped",
    "lost",
    "unknown",
]


class JobInfo(BaseModel):
    """One tracked command or managed operation owned by an explicit agent session."""

    job_id: str = Field(
        description="Stable tracked job identifier. Use this with the `job` companion poll, cancel, or retry actions in the same session."
    )
    kind: Literal["shell", "managed"] = Field(
        default="shell",
        description="Execution kind. Shell jobs use a persistent shell runner; managed jobs run a registered controller operation.",
    )
    name: str = Field(
        description="Human-readable job name, or the generated job_id when no name was provided."
    )
    status: JobStatus = Field(
        description="Durable tracked-job lifecycle state, including transitional, successful, failed, stopped, and recovery states."
    )
    command: str = Field(
        description="Original shell command or managed-operation summary. Retry reuses the stored operation."
    )
    cwd: str = Field(
        description="Working directory or display context associated with the tracked job."
    )
    session_id: str = Field(
        description="Agent/workspace session_id that owns this tracked job."
    )
    progress: dict[str, object] | None = Field(
        default=None,
        description="Latest bounded structured progress snapshot for a managed job.",
    )
    result: dict[str, object] | None = Field(
        default=None,
        description="Bounded structured result retained after a managed job succeeds.",
    )
    created_at: float = Field(
        description="Unix timestamp when the tracked job record was created."
    )
    updated_at: float = Field(
        description="Unix timestamp when the tracked job record was last updated."
    )
    last_started_at: float | None = Field(
        default=None,
        description="Unix timestamp when the current or most recent attempt was started.",
    )
    completed_at: float | None = Field(
        default=None,
        description="Unix timestamp when the current attempt reached a terminal state.",
    )
    exit_code: int | None = Field(
        default=None,
        description="Durably recorded exit code for shell or managed execution when available.",
    )
    error: str | None = Field(
        default=None,
        description="Lifecycle, shell-runner, or managed-operation error for the current attempt.",
    )
    log_truncated: bool = Field(
        default=False,
        description="Whether older output was discarded to enforce max_job_log_bytes.",
    )
    output_bytes: int = Field(
        default=0,
        description="Total durable log bytes observed before tail truncation.",
    )
    attempts: int = Field(
        description="Number of times this tracked job has been started, including retries."
    )


class JobStartOutput(JobInfo):
    """Tracked job record created after starting shell or managed work."""


class JobRetryOutput(JobInfo):
    """Tracked job record after restarting its durable command or managed payload."""


class JobListOutput(BaseModel):
    """Tracked job inventory for one agent session."""

    jobs: list[JobInfo] = Field(
        description="Tracked jobs for the requested session, sorted newest first, optionally excluding terminal jobs."
    )
    counts: dict[str, int] = Field(
        description="Counts for tracked jobs owned by the requested session, before include_finished filtering."
    )


class JobTailOutput(BaseModel):
    """Recent bounded durable output for one tracked job."""

    job: JobInfo = Field(
        description="Tracked job metadata after status refresh."
    )
    output: str = Field(
        default="",
        description="Recent output from the durable per-attempt job log, including after process exit.",
    )
    message: str | None = Field(
        default=None,
        description="Diagnostic message when output or an executor snapshot is unavailable.",
    )


class JobStopOutput(BaseModel):
    """Result of stopping one tracked job."""

    job: JobInfo = Field(description="Tracked job metadata after stop attempt.")
    killed: bool = Field(description="Whether the tracked job was stopped.")
    stderr: str = Field(
        default="",
        description="Backend stderr from a shell stop attempt; managed cancellation normally leaves this empty.",
    )


class JobOutput(BaseModel):
    """Unified companion result for tracked shell and managed jobs."""

    operation: Literal["list", "poll", "cancel", "retry"] = Field(
        description="Job operation performed by the unified job companion tool."
    )
    jobs: list[JobInfo] = Field(
        default_factory=list,
        description="Tracked job rows returned for list-style snapshots.",
    )
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="Tracked job counts by status when a list snapshot was read.",
    )
    outputs: list[JobTailOutput] = Field(
        default_factory=list,
        description="Recent output/status entries returned for inspected jobs.",
    )
    cancelled: list[JobStopOutput] = Field(
        default_factory=list,
        description="Stop results returned for cancelled jobs.",
    )
    retried: list[JobRetryOutput] = Field(
        default_factory=list,
        description="Restarted job rows returned for retried jobs.",
    )
    message: str | None = Field(
        default=None,
        description="Optional diagnostic or usage note.",
    )
