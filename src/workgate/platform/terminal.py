"""Choose a terminal attachment implementation by native host capability."""

from collections.abc import Callable

from ..protocol.terminal import TerminalBridgeUnsupportedError
from .contracts import TerminalAttachment
from .host import HostOS, host_os


def supports_raw_terminal_host(*, system: str | None = None) -> bool:
    """Whether this host has a native raw terminal implementation."""
    current = host_os(system)
    if current is HostOS.WINDOWS:
        from .windows_conpty import is_available

        return is_available()
    return current in (HostOS.LINUX, HostOS.MACOS)


def terminal_attachment_factory(
    *, system: str | None = None
) -> Callable[[str, int, int], TerminalAttachment]:
    """Return a native attachment type, or report an unavailable backend."""
    current = host_os(system)
    if current in (HostOS.LINUX, HostOS.MACOS):
        from .posix.terminal import UnixTmuxAttach

        return UnixTmuxAttach
    if current is HostOS.WINDOWS and supports_raw_terminal_host(system=system):
        from .windows_conpty import WindowsConPtyAttach

        return WindowsConPtyAttach
    raise TerminalBridgeUnsupportedError(
        f"Raw terminal attachment is unsupported on {system or current or 'this host'}"
    )
