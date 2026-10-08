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
    return """Start a session bound to an eligible executor and workdir. Supply task_id to attach an existing durable task; a task does not select the executor or workdir."""


def _session_change_workdir_description(_context: McpToolContext) -> str:
    return """Change the execution workdir for this session; task identity and executor binding stay unchanged."""


def _session_copy_description(_context: McpToolContext) -> str:
    return """Copy files or directories between sessions, including across executors. Runs as a managed job by default; set background=false for a synchronous result. Overwrite may replace the destination."""


def _session_end_description(_context: McpToolContext) -> str:
    return """End one execution session and stop its owned work. An attached task remains available; files are not deleted."""


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
    http_path="/tools/session_change_workdir",
    description=_session_change_workdir_description,
    oauth_scopes=("shell:read",),
)
async def session_change_workdir(
    session_id: SessionIdArg,
    workdir: SessionWorkdirArg,
) -> SessionStartOutput:
    """Change an execution session workdir."""
    raise _unrouted_session_tool("session_change_workdir")


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
    overwrite: SessionCopyOverwriteArg = False,
    chunk_size: SessionCopyChunkSizeArg = None,
    background: SessionCopyBackgroundArg = True,
) -> SessionCopyOutput | JobStartOutput:
    """Copy a file or directory synchronously or as a managed job."""
    raise _unrouted_session_tool("session_copy")
