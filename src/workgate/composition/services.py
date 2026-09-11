"""Control-owned process service composition."""

from dataclasses import dataclass

from ..config.control import ControlSettingsView
from ..persistence import FileStateStore, StateStore, configure_state_store


@dataclass(frozen=True)
class ControlServices:
    """Private durable services owned by one control process."""

    state_store: FileStateStore


@dataclass
class ControlServiceInstallation:
    """One reversible control state-store compatibility binding."""

    previous_state_store: StateStore | None
    _active: bool = True

    def close(self) -> None:
        if not self._active:
            return
        configure_state_store(self.previous_state_store)
        self._active = False


def build_control_services(settings: ControlSettingsView) -> ControlServices:
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
