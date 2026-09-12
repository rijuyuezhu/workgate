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
        f"{name} requires the control-plane session router; direct local execution is disabled"
    )


class SessionToolRegistry(DeclarativeToolRegistry):
    """Register explicit agent session tools."""

    name = "session"
    """Registry group name used for tool-surface organization."""


session_tool = SessionToolRegistry.get_tool_decorator()


def _session_start_description(_context: McpToolContext) -> str:
    return """Start an explicit agent/workspace session on an executor and bind it to a required workdir. Omit executor_id only when exactly one trusted, non-revoked, protocol-compatible session-capable executor is currently online; otherwise pass the stable executor_id explicitly. The control plane allocates one opaque shared session_id and the executor stores the same id. Before calling, infer the most specific safe project workdir from the task. Pass the returned session_id to every machine-facing workspace tool."""


def _session_change_cwd_description(_context: McpToolContext) -> str:
    return """Change an existing executor-backed agent/workspace session to a new required workdir. Relative workdirs resolve against the executor's fixed workspace_root; old grounding snapshots are invalidated before the durable cwd changes. Use this when the user redirects you to a different project/subdirectory."""


def _session_copy_description(_context: McpToolContext) -> str:
    return """Copy one file or directory between two existing executor-backed agent/workspace sessions. Paths resolve inside their respective session workdirs and neither session is created, migrated, or rebound. Same-executor copies involve one executor; cross-executor copies use control-coordinated transfer between the two bound executors without using the control workspace as a filesystem endpoint. By default the call waits and returns SessionCopyOutput. Set background=true for long transfers to return a managed job immediately, then use the job companion with src_session_id to poll structured progress, cancel safely, or retry according to transfer-specific semantics. The response reports whether both endpoints share an executor."""


def _session_end_description(_context: McpToolContext) -> str:
    return """End one explicit executor-backed agent/workspace session. The control plane first persists desired termination, stops owned tracked jobs and persistent PTYs as required, and asks the bound executor to make the shared session absent. If the executor is permanently unreachable, force=true explicitly releases only the control binding and reports that executor cleanup was not confirmed. Use session_end when a task is complete so durable capacity is released without restarting the server. This is destructive for running work in that session but does not delete workspace files."""


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
