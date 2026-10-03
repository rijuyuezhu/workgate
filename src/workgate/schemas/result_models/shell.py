"""Typed structured outputs for shell-facing tools."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PersistentShellBackend = str


class CommandResult(BaseModel):
    """Completed subprocess result."""

    ok: bool = Field(
        description="Whether the subprocess exited with code 0 before timeout."
    )
    exit_code: int | None = Field(
        description="Process exit code, or null when no process exited."
    )
    timed_out: bool = Field(
        default=False,
        description="Whether execution was terminated after exceeding its timeout.",
    )
    duration_ms: int = Field(
        description="Elapsed command runtime in milliseconds."
    )
    cwd: str = Field(
        description="Working directory used to run the command, displayed relative to the workspace when possible."
    )
    command: str = Field(description="Shell command string that was executed.")
    stdout: str = Field(
        default="",
        description="Captured standard output after byte-limit truncation.",
    )
    stderr: str = Field(
        default="",
        description="Captured standard error after byte-limit truncation.",
    )
    truncated: bool = Field(
        default=False,
        description="Whether stdout or stderr was truncated to fit output limits.",
    )


class RunShellCommandOutput(CommandResult):
    """Result of running one bounded non-interactive shell command."""


class ShellExecutionOutput(BaseModel):
    """Result returned by the bash shell execution tool."""

    mode: Literal["command", "job", "pty"] = Field(
        description="Execution mode selected by the bash tool."
    )
    command: str = Field(description="Shell command submitted by the caller.")
    cwd: str = Field(
        description="Resolved working directory used for this shell execution."
    )
    result: dict[str, Any] = Field(
        description="Structured result from the selected shell mode: bounded command output, async job metadata with owning execution session_id and job_id, or PTY metadata with shell_id for persistent-shell companion tools."
    )


class RunPythonCodeOutput(BaseModel):
    """Result of writing Python code to a temporary file and executing it through shell modes."""

    mode: Literal["command", "job", "pty"] = Field(
        description="Execution mode selected for the generated Python script."
    )
    command: str = Field(
        description="Generated shell command used to run the temporary Python script."
    )
    cwd: str = Field(
        description="Resolved working directory used for this Python execution."
    )
    result: dict[str, Any] = Field(
        description="Structured result from the selected shell mode: bounded command output, async job metadata with owning execution session_id and job_id, or PTY metadata with shell_id for persistent-shell companion tools."
    )
    script_path: str = Field(
        description="Path to the temporary Python script that was executed."
    )


class StartPersistentShellOutput(BaseModel):
    """Result of starting a persistent shell."""

    model_config = ConfigDict(extra="allow")
    """Allow passthrough keys for dynamically shaped output payloads."""

    shell_id: str = Field(
        description="Identifier used by later persistent-shell tools to send input, read output, or kill the shell."
    )
    name: str | None = Field(
        default=None,
        description="Optional human-readable shell label, when available.",
    )
    cwd: str | None = Field(
        default=None,
        description="Working directory where the persistent shell was started.",
    )
    command: str | None = Field(
        default=None,
        description="Initial command launched in the persistent shell, or the configured shell executable when omitted.",
    )
    backend: PersistentShellBackend | None = Field(
        default=None,
        description="Persistent-shell backend selected for this session.",
    )


class SendPersistentShellInputOutput(BaseModel):
    """Result of sending input to a persistent shell."""

    shell_id: str = Field(description="Persistent shell that received input.")
    sent_bytes: int = Field(
        description="Number of UTF-8 bytes sent to the persistent shell."
    )
    enter: bool = Field(
        description="Whether an Enter key was sent after the input text."
    )


class ResizePersistentShellOutput(BaseModel):
    """Result of resizing a persistent terminal."""

    shell_id: str = Field(description="Persistent shell that was resized.")
    cols: int = Field(
        description="Applied terminal width in character columns."
    )
    rows: int = Field(description="Applied terminal height in character rows.")
    resized: bool = Field(
        description="Whether the backend applied the requested size."
    )
    backend: PersistentShellBackend = Field(
        description="Persistent-terminal backend that handled the resize request."
    )


class ReadPersistentShellOutput(BaseModel):
    """Recent output captured from a persistent shell."""

    model_config = ConfigDict(extra="allow")
    """Allow passthrough keys for dynamically shaped output payloads."""

    shell_id: str = Field(description="Persistent shell that was read.")
    output: str = Field(
        default="",
        description="Captured recent terminal output from the shell.",
    )
    lines: int | None = Field(
        default=None,
        description="Requested number of recent terminal lines, when returned by the implementation.",
    )
    backend: PersistentShellBackend | None = Field(
        default=None,
        description="Persistent-shell backend that produced the output.",
    )


class KillPersistentShellOutput(BaseModel):
    """Result of terminating a persistent shell."""

    model_config = ConfigDict(extra="allow")
    """Allow passthrough keys for dynamically shaped output payloads."""

    shell_id: str = Field(
        description="Persistent shell targeted for termination."
    )
    killed: bool | None = Field(
        default=None,
        description="Whether the persistent shell was killed successfully.",
    )
    stderr: str | None = Field(
        default=None,
        description="Captured backend error text from the kill operation, when available.",
    )
    backend: PersistentShellBackend | None = Field(
        default=None,
        description="Persistent-shell backend that handled termination.",
    )


class PersistentShellInfo(BaseModel):
    """One persistent shell entry."""

    model_config = ConfigDict(extra="allow")
    """Allow passthrough keys for dynamically shaped output payloads."""

    shell_id: str = Field(description="Persistent shell identifier.")
    name: str | None = Field(
        default=None, description="Optional human-readable shell label."
    )
    cwd: str | None = Field(
        default=None,
        description="Current or initial working directory, when known.",
    )
    command: str | None = Field(
        default=None, description="Initial or current command, when known."
    )
    backend: PersistentShellBackend | None = Field(
        default=None,
        description="Persistent-shell backend that owns this session.",
    )


class ListPersistentShellsOutput(BaseModel):
    """Active persistent shells."""

    model_config = ConfigDict(extra="allow")
    """Allow passthrough keys for dynamically shaped output payloads."""

    shells: list[PersistentShellInfo] = Field(
        description="Active persistent shells with at least shell_id and optional implementation-specific metadata."
    )
