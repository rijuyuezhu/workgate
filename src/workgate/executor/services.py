"""Executor-owned durable state and machine-session service composition."""

from dataclasses import dataclass

from ..config.executor import ExecutorConfig
from ..persistence import FileStateStore
from .tool_session.store import SessionPathResolver, ToolSessionStore


@dataclass(frozen=True)
class RuntimeServices:
    """Executor process services that include machine-session authority."""

    state_store: FileStateStore
    tool_session_store: ToolSessionStore


def build_runtime_services(
    config: ExecutorConfig,
    *,
    path_resolver: SessionPathResolver | None = None,
) -> RuntimeServices:
    """Construct executor state and machine-session services without installing them."""
    state_store = FileStateStore(lambda: config.state_dir)
    tool_session_store = ToolSessionStore(
        state_store=state_store,
        path_resolver=path_resolver,
        workspace_root=config.workspace_root,
        allow_full_control=config.allow_full_control,
        path_denylist=config.path_denylist,
        max_session_snapshots=config.max_session_snapshots,
        max_session_snapshot_bytes=config.max_session_snapshot_bytes,
    )
    return RuntimeServices(
        state_store=state_store,
        tool_session_store=tool_session_store,
    )
