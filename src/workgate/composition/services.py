"""Role-specific construction and compatibility installation of process services."""

from dataclasses import dataclass

from ..config.settings import Settings
from ..persistence import FileStateStore, StateStore, configure_state_store
from ..tool_session import configure_tool_session_store
from ..tool_session.store import SessionPathResolver, ToolSessionStore


@dataclass(frozen=True)
class ControlServices:
    """Private durable services owned by one control process."""

    state_store: FileStateStore
    """Configured control-owned durable state store."""


@dataclass
class ControlServiceInstallation:
    """One reversible control state-store compatibility binding."""

    previous_state_store: StateStore | None
    _active: bool = True

    def close(self) -> None:
        """Restore the previous state-store binding exactly once."""
        if not self._active:
            return
        configure_state_store(self.previous_state_store)
        self._active = False


@dataclass(frozen=True)
class RuntimeServices:
    """Executor process services that include machine-session authority."""

    state_store: FileStateStore
    """Configured durable executor state store."""
    tool_session_store: ToolSessionStore
    """Configured authoritative executor tool-session store."""


@dataclass
class RuntimeServiceInstallation:
    """One reversible executor compatibility-global installation."""

    previous_state_store: StateStore | None
    """State-store binding that was active before this installation."""
    previous_tool_session_store: ToolSessionStore | None
    """Tool-session binding that was active before this installation."""
    _active: bool = True

    def close(self) -> None:
        """Restore the previous compatibility bindings exactly once."""
        if not self._active:
            return
        configure_tool_session_store(self.previous_tool_session_store)
        configure_state_store(self.previous_state_store)
        self._active = False


def build_control_services(settings: Settings) -> ControlServices:
    """Construct control-private state without executor workspace/session authority."""
    return ControlServices(
        state_store=FileStateStore(lambda: settings.state_dir)
    )


def install_control_services(
    services: ControlServices,
) -> ControlServiceInstallation:
    """Install only the control-owned state-store compatibility accessor."""
    return ControlServiceInstallation(
        previous_state_store=configure_state_store(services.state_store)
    )


def build_runtime_services(
    settings: Settings,
    *,
    path_resolver: SessionPathResolver | None = None,
) -> RuntimeServices:
    """Construct executor state and machine-session services without installing them."""
    state_store = FileStateStore(lambda: settings.state_dir)
    tool_session_store = ToolSessionStore(
        state_store=state_store,
        settings_provider=lambda: settings,
        path_resolver=path_resolver,
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
