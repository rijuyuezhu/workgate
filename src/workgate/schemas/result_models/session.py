"""Typed structured outputs for execution sessions."""

from typing import Literal

from pydantic import BaseModel, Field


class GitSessionInfo(BaseModel):
    """Lightweight git orientation for a session workdir."""

    is_repo: bool = Field(
        description="Whether the session workdir is inside a git repository."
    )
    root: str | None = Field(
        default=None, description="Git repository root, if available."
    )
    branch: str | None = Field(
        default=None,
        description="Current branch or short commit name, if available.",
    )
    dirty: bool | None = Field(
        default=None,
        description="Whether git reports uncommitted changes, if available.",
    )


class SessionRuntimeEnvironment(BaseModel):
    """Runtime identity safe to expose for tool selection."""

    workgate_version: str = Field(
        description="Running workgate source version."
    )
    package_version: str = Field(
        description="Installed workgate distribution version."
    )
    python_implementation: str = Field(
        description="Python implementation name, such as CPython."
    )
    python_version: str = Field(description="Python language runtime version.")
    runtime_kind: Literal["source", "frozen"] = Field(
        description="Whether this process runs from source or a frozen executable."
    )
    os: str = Field(description="Normalized operating-system family.")
    release: str = Field(description="Bounded operating-system release string.")
    architecture: str = Field(description="Normalized machine architecture.")
    process_bits: Literal[32, 64] = Field(
        description="Python process pointer width in bits."
    )


class SessionToolProbe(BaseModel):
    """Bounded availability and version result for one allowlisted tool."""

    available: bool = Field(description="Whether the tool can be selected.")
    status: Literal[
        "available", "missing", "timeout", "error", "unsupported"
    ] = Field(description="Normalized probe status without raw exception text.")
    version: str | None = Field(
        default=None,
        description="Extracted bounded version token, when available.",
    )
    source: str | None = Field(
        default=None,
        description="Safe resolution source such as configured, system, or bundled.",
    )


class SessionToolsEnvironment(BaseModel):
    """Allowlisted executable probes owned by the session executor."""

    shell: SessionToolProbe = Field(description="Configured shell probe.")
    git: SessionToolProbe = Field(description="Git probe.")
    ripgrep: SessionToolProbe = Field(description="ripgrep probe.")
    tmux: SessionToolProbe = Field(description="tmux backend probe.")


class SessionCapabilitiesEnvironment(BaseModel):
    """Executor-local feature support relevant to choosing session tools."""

    raw_pty: bool = Field(
        description="Whether a raw persistent-terminal backend is available."
    )
    conpty: bool = Field(description="Whether Windows ConPTY is available.")
    browser: bool = Field(
        description="Whether structured Playwright browser automation is available on this executor."
    )
    gui: bool = Field(
        description="Whether native desktop GUI automation is available on this executor."
    )


class SessionLimits(BaseModel):
    """Executor-owned limits relevant to tool selection."""

    shell_default_timeout_s: int = Field(
        description="Default bounded shell timeout in seconds."
    )
    shell_max_timeout_s: int = Field(
        description="Maximum bounded shell timeout in seconds."
    )
    max_output_bytes: int = Field(
        description="Maximum bounded command output bytes."
    )
    max_jobs: int = Field(
        description="Maximum retained tracked shell-job records."
    )
    max_job_log_bytes: int = Field(
        description="Maximum durable log bytes retained for one shell job."
    )
    max_session_snapshots: int = Field(
        description="Maximum grounding snapshots retained per session."
    )
    max_session_snapshot_bytes: int = Field(
        description="Maximum grounding-snapshot metadata bytes per session."
    )
    max_file_read_bytes: int = Field(
        description="Maximum bytes read from one file."
    )
    max_file_write_bytes: int = Field(
        description="Maximum bytes written to one file."
    )
    max_view_image_bytes: int = Field(
        description="Maximum image bytes accepted by view_image."
    )
    max_search_results: int = Field(
        description="Maximum text-search result count."
    )
    max_glob_results: int = Field(
        description="Maximum glob-search result count."
    )
    max_tree_entries: int = Field(description="Maximum tree-view entry count.")
    max_directory_entries: int = Field(
        description="Maximum directory entries returned by one listing."
    )
    max_concurrent_commands: int = Field(
        description="Concurrent executor command limit."
    )
    max_persistent_shells: int = Field(
        description="Persistent-shell session limit."
    )
    max_transfer_archive_entries: int = Field(
        description="Maximum entries accepted from one transfer archive."
    )
    max_transfer_unpacked_bytes: int = Field(
        description="Maximum unpacked regular-file bytes for one transfer."
    )


class SessionEnvironment(BaseModel):
    """Structured, bounded environment orientation for one session target."""

    runtime: SessionRuntimeEnvironment = Field(description="Runtime identity.")
    tools: SessionToolsEnvironment = Field(
        description="Allowlisted tool probes."
    )
    capabilities: SessionCapabilitiesEnvironment = Field(
        description="Effective capabilities."
    )
    limits: SessionLimits = Field(description="Effective executor limits.")


class SessionStartOutput(BaseModel):
    """Execution-session orientation."""

    session_id: str = Field(
        description=(
            "Opaque execution session id with at least 128 bits of randomness."
        )
    )
    executor_id: str | None = Field(
        default=None,
        description="Stable executor id bound to this execution session.",
    )
    task_id: str | None = Field(
        default=None,
        description="Optional semantic task explicitly attached to this execution session.",
    )
    workdir: str = Field(description="Canonical workdir bound to this session.")
    created_at: float = Field(
        description="Unix timestamp when the session was created."
    )
    updated_at: float = Field(
        description="Unix timestamp when the session was last touched."
    )
    label: str | None = Field(
        default=None, description="Optional human-readable session label."
    )
    default_workdir: str = Field(
        description="Effective default workdir reported by the executor that owns this session."
    )
    git: GitSessionInfo = Field(
        description="Lightweight git orientation for the session workdir."
    )
    instruction_files: list[str] = Field(
        description="Project instruction files discovered from the session workdir through its ancestors."
    )
    environment: SessionEnvironment = Field(
        description="Bounded runtime, tool, capability, and limit orientation."
    )
    message: str = Field(
        description="Short model-facing instruction for using this session."
    )


class SessionEndOutput(BaseModel):
    """Result of ending one execution session."""

    session_id: str = Field(description="Ended execution session id.")
    executor_id: str | None = Field(
        default=None,
        description="Stable executor id formerly bound to this execution session, when available.",
    )
    ended: bool = Field(
        description="Whether durable session state was removed."
    )
    stopped_jobs: list[str] = Field(
        default_factory=list,
        description="Tracked job ids stopped before session removal.",
    )
    stopped_shells: list[str] = Field(
        default_factory=list,
        description="Persistent shell ids stopped before session removal.",
    )
    stopped_browsers: list[str] = Field(
        default_factory=list,
        description="Ephemeral browser session ids stopped before session removal.",
    )
    force_released: bool = Field(
        default=False,
        description="Whether control released the binding without confirmed executor cleanup.",
    )


class SessionCopyEndpoint(BaseModel):
    """One endpoint in a session-to-session copy."""

    session_id: str = Field(
        description="Execution session id for this endpoint."
    )
    executor_id: str = Field(description="Executor identity for this endpoint.")
    workdir: str = Field(
        description="Session workdir used for path resolution."
    )
    path: str = Field(
        description="Caller-provided path; relative values resolve from the session workdir."
    )


class SessionCopyRelation(BaseModel):
    """Relationship between the source and destination sessions."""

    route: Literal["same_executor", "different_executors"] = Field(
        description="Relationship between the source and destination executors."
    )
    same_session: bool = Field(
        description="Whether source and destination are the same execution session."
    )
    same_executor: bool = Field(
        description="Whether both execution sessions are bound to the same executor."
    )


class SessionCopyOutput(BaseModel):
    """Result of copying a file or directory between two sessions."""

    kind: Literal["file", "dir"] = Field(
        description="Resolved copied object kind."
    )
    transport: Literal["same_executor", "object_store", "control_relay"] = (
        Field(description="Actual data route selected for the copy operation.")
    )
    resumed_bytes: int = Field(
        default=0,
        description="Bytes reused from a validated transfer-specific import checkpoint.",
    )
    source: SessionCopyEndpoint = Field(description="Source copy endpoint.")
    destination: SessionCopyEndpoint = Field(
        description="Destination copy endpoint."
    )
    relation: SessionCopyRelation = Field(
        description="Analyzed relationship between the two sessions."
    )
    bytes: int | None = Field(
        default=None, description="Number of file bytes copied for file copies."
    )
    sha256: str | None = Field(
        default=None,
        description="SHA-256 digest for file copies, when available.",
    )
    archive_bytes: int | None = Field(
        default=None, description="Transfer archive size for directory copies."
    )
    archive_sha256: str | None = Field(
        default=None,
        description="Transfer archive digest for directory copies.",
    )
    chunks: int = Field(
        description="Number of logical binary chunks in the transfer payload."
    )
    chunk_size: int = Field(
        description="Logical binary chunk size used by the transfer client."
    )
    entries: int | None = Field(
        default=None,
        description="Number of directory entries unpacked for directory copies.",
    )
    cleanup_errors: list[str] = Field(
        default_factory=list,
        description="Non-fatal cleanup errors after a successful copy commit.",
    )
    fallbacks: list[str] = Field(
        default_factory=list,
        description="Bounded route failures observed before the selected route succeeded.",
    )
