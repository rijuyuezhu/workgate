"""Execution session tool registry."""

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
    SessionStartWorkdirArg,
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
    """Register execution session tools."""

    name = "session"
    """Registry group name used for tool-surface organization."""


session_tool = SessionToolRegistry.get_tool_decorator()


def _session_start_description(_context: McpToolContext) -> str:
    return """Start an execution session on one executor. Omit workdir to use the executor default; relative workdirs resolve from that default. Optionally attach an existing task_id. Omit executor_id only when exactly one eligible executor is online."""


def _session_change_cwd_description(_context: McpToolContext) -> str:
    return """Change an execution session's workdir. Relative paths resolve against the executor default workdir, and old grounding snapshots are invalidated."""


def _session_copy_description(_context: McpToolContext) -> str:
    return """Copy one file or directory between two existing execution sessions. Relative paths resolve from their session workdirs. Set background=true to return a managed job owned by src_session_id."""


def _session_end_description(_context: McpToolContext) -> str:
    return """End one executor-backed execution session. This stops owned work and releases execution capacity; an attached task remains independently readable and mutable. Workspace files are not deleted."""


@session_tool(
    http_method="POST",
    http_path="/tools/session_start",
    description=_session_start_description,
    oauth_scopes=("shell:read",),
)
async def session_start(
    workdir: SessionStartWorkdirArg = None,
    label: SessionLabelArg = None,
    executor_id: SessionExecutorIdArg = None,
    task_id: SessionTaskIdArg = None,
) -> SessionStartOutput:
    """Start an execution session."""
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
    """Change an execution session workdir."""
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
    """Stop owned work and end an execution session."""
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
