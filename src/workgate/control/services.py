"""Control-owned process service composition."""

from dataclasses import dataclass

from ..config.control import ControlConfig
from ..persistence import FileStateStore


@dataclass(frozen=True)
class ControlServices:
    """Private durable services owned by one control process."""

    state_store: FileStateStore


def build_control_services(settings: ControlConfig) -> ControlServices:
    """Construct control-private state without executor workspace/session authority."""
    return ControlServices(
        state_store=FileStateStore(lambda: settings.state_dir)
    )
