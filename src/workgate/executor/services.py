"""Executor-owned durable state and machine-session service composition."""

from dataclasses import dataclass

from ..persistence import FileStateStore, StateStore, configure_state_store
from .config import ExecutorConfig
from .tool_session import configure_tool_session_store
from .tool_session.store import SessionPathResolver, ToolSessionStore


@dataclass(frozen=True)
class RuntimeServices:
    """Executor process services that include machine-session authority."""

    state_store: FileStateStore
    tool_session_store: ToolSessionStore


@dataclass
class RuntimeServiceInstallation:
    """One reversible executor compatibility-global installation."""

    previous_state_store: StateStore | None
    previous_tool_session_store: ToolSessionStore | None
    _active: bool = True

    def close(self) -> None:
        if not self._active:
            return
        configure_tool_session_store(self.previous_tool_session_store)
        configure_state_store(self.previous_state_store)
        self._active = False


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


def install_runtime_services(
    services: RuntimeServices,
) -> RuntimeServiceInstallation:
    """Install executor state/session compatibility accessors."""
    previous_state_store = configure_state_store(services.state_store)
    try:
        previous_tool_session_store = configure_tool_session_store(
            services.tool_session_store
        )
    except BaseException:
        configure_state_store(previous_state_store)
        raise
    return RuntimeServiceInstallation(
        previous_state_store=previous_state_store,
        previous_tool_session_store=previous_tool_session_store,
    )
