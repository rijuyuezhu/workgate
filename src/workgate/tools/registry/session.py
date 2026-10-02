"""Explicit agent session tool registry."""

from ...schemas.input_models.session import (
    SessionCopyBackgroundArg,
    SessionCopyChunkSizeArg,
    SessionCopyKindArg,
    SessionCopyOverwriteArg,
    SessionCopyPathArg,
    SessionEndForceArg,
    SessionExecutorIdArg,
    SessionIdArg,
    SessionLabelArg,
    SessionTaskIdArg,
    SessionWorkdirArg,
)
from ...schemas.result_models.jobs import JobStartOutput
from ...schemas.result_models.session import (
    SessionCopyOutput,
    SessionEndOutput,
    SessionStartOutput,
)
from ..contracts import McpToolContext
from ..declarative import DeclarativeToolRegistry


def _unrouted_session_tool(name: str) -> RuntimeError:
    return RuntimeError(
        f"{name} requires the control-plane session router; machine execution is executor-routed"
    )


class SessionToolRegistry(DeclarativeToolRegistry):
    """Register explicit agent session tools."""

    name = "session"
    """Registry group name used for tool-surface organization."""


session_tool = SessionToolRegistry.get_tool_decorator()


def _session_start_description(_context: McpToolContext) -> str:
    return """Start an explicit execution session on one executor and bind it to a required workdir. Optionally attach it to an existing semantic task_id; that attachment never chooses or changes the executor/workdir. Omit executor_id only when exactly one eligible executor is online. Pass the returned session_id to machine-facing tools."""


def _session_change_cwd_description(_context: McpToolContext) -> str:
    return """Change an existing executor-backed agent/workspace session to a new required workdir. Relative workdirs resolve against the executor's fixed workspace_root; old grounding snapshots are invalidated before the durable cwd changes. Use this when the user redirects you to a different project/subdirectory."""


def _session_copy_description(_context: McpToolContext) -> str:
    return """Copy one file or directory between two existing executor-backed agent/workspace sessions. Paths resolve inside their respective session workdirs and neither session is created, migrated, or rebound. Same-executor copies involve one executor; cross-executor copies export an immutable verified payload owned by control and import it into the existing destination session without using the control workspace as a filesystem endpoint. By default the call waits and returns SessionCopyOutput. Set background=true for long transfers to return a managed job immediately, then use the job companion with src_session_id to poll structured progress, cancel safely, or retry according to transfer-specific semantics. The response reports whether both endpoints share an executor."""


def _session_end_description(_context: McpToolContext) -> str:
    return """End one explicit executor-backed execution session. This stops owned work and releases execution capacity but does not finish, cancel, freeze, or delete an attached semantic task. The task remains independently readable and mutable by task_id. This is destructive for running work in that session but does not delete workspace files."""


@session_tool(
    http_method="POST",
    http_path="/tools/session_start",
    description=_session_start_description,
    oauth_scopes=("shell:read",),
)
async def session_start(
    workdir: SessionWorkdirArg,
    label: SessionLabelArg = None,
    executor_id: SessionExecutorIdArg = None,
    task_id: SessionTaskIdArg = None,
) -> SessionStartOutput:
    """Start an explicit agent/workspace session."""
    raise _unrouted_session_tool("session_start")


@session_tool(
    http_method="POST",
    http_path="/tools/session_change_cwd",
    description=_session_change_cwd_description,
    oauth_scopes=("shell:read",),
)
async def session_change_cwd(
    session_id: SessionIdArg,
    workdir: SessionWorkdirArg,
) -> SessionStartOutput:
    """Change an explicit agent/workspace session workdir."""
    raise _unrouted_session_tool("session_change_cwd")


@session_tool(
    http_method="POST",
    http_path="/tools/session_end",
    description=_session_end_description,
    oauth_scopes=("shell:execute",),
)
async def session_end(
    session_id: SessionIdArg, force: SessionEndForceArg = False
) -> SessionEndOutput:
    """Stop owned work and end an explicit agent/workspace session."""
    raise _unrouted_session_tool("session_end")


@session_tool(
    http_method="POST",
    http_path="/tools/session_copy",
    description=_session_copy_description,
    oauth_scopes=("shell:read", "shell:write"),
)
async def session_copy(
    src_session_id: SessionIdArg,
    src_path: SessionCopyPathArg,
    dst_session_id: SessionIdArg,
    dst_path: SessionCopyPathArg,
    kind: SessionCopyKindArg = "auto",
    overwrite: SessionCopyOverwriteArg = True,
    chunk_size: SessionCopyChunkSizeArg = None,
    background: SessionCopyBackgroundArg = False,
) -> SessionCopyOutput | JobStartOutput:
    """Copy a file or directory synchronously or as a managed job."""
    raise _unrouted_session_tool("session_copy")
