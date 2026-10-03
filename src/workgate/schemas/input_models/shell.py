"""Typed input annotations for shell-facing tools."""

from typing import Annotated

from pydantic import Field

ShellCommandArg = Annotated[
    str,
    Field(
        description="Shell command string to execute for terminal work such as tests, builds, package managers, git, or scripts."
    ),
]
ShellCwdArg = Annotated[
    str,
    Field(
        description="Optional working directory for the command. Relative values resolve from the execution session workdir; omit or pass . to use it."
    ),
]
ShellTimeoutArg = Annotated[
    int | None,
    Field(
        description="Optional timeout in seconds for bounded command mode. For long-running work, prefer async_=true and manage the returned job_id with job."
    ),
]
ShellMaxOutputBytesArg = Annotated[
    int | None,
    Field(
        description="Optional combined stdout/stderr byte budget for bounded command mode. Values above the configured server cap are clamped."
    ),
]
ShellEnvArg = Annotated[
    dict[str, str] | None,
    Field(
        description="Optional environment variables for multiline, quote-heavy, or caller-provided values; reference them from the command instead of embedding them directly."
    ),
]
ShellAsyncArg = Annotated[
    bool,
    Field(
        description="Whether to start long-running non-interactive work as a tracked background job owned by this session. Returns job_id for the job tool, not shell_id."
    ),
]
ShellPtyArg = Annotated[
    bool,
    Field(
        description="Whether to start the command in a persistent PTY shell for interactive programs, REPLs, servers, or commands needing later input. PTY mode returns shell_id for persistent-shell companion tools, not job_id."
    ),
]
ShellNameArg = Annotated[
    str | None,
    Field(
        description="Optional name for the tracked async job or persistent PTY shell."
    ),
]
PythonCodeArg = Annotated[
    str,
    Field(
        description="Complete Python source code to write to a temporary script and execute through the shell execution surface."
    ),
]
ShellIdArg = Annotated[
    str,
    Field(
        description="Persistent shell_id returned by bash(pty=true) or list_persistent_shells. This is not the execution session_id."
    ),
]
InputTextArg = Annotated[
    str, Field(description="Text to send to the persistent shell.")
]
EnterArg = Annotated[
    bool, Field(description="Whether to send Enter after the input text.")
]
LinesArg = Annotated[
    int,
    Field(
        description="Number of recent terminal lines to capture from the persistent shell."
    ),
]
ShellColumnsArg = Annotated[
    int,
    Field(
        ge=20,
        le=1600,
        description="Persistent terminal width in character columns (20-1600).",
    ),
]
ShellRowsArg = Annotated[
    int,
    Field(
        ge=3,
        le=500,
        description="Persistent terminal height in character rows (3-500).",
    ),
]
ToolPurposeArg = Annotated[
    str | None,
    Field(
        default=None,
        description="Optional short purpose explaining why this tool call is being made. Maximum 500 characters.",
    ),
]
