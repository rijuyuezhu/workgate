"""Executor-owned native desktop GUI automation."""

import importlib
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from functools import lru_cache

from ...config.executor import ExecutorConfig
from ..tool_session.store import ToolSessionStore
from .base import GuiBackend, GuiService


def _backend_for_platform() -> GuiBackend:
    system = platform.system()
    if system == "Darwin":
        from .macos import MacOSGuiBackend

        return MacOSGuiBackend()
    if system == "Linux":
        from .linux import LinuxGuiBackend

        return LinuxGuiBackend()
    from ...errors import GuiUnavailableError

    raise GuiUnavailableError(
        f"GUI automation is unsupported on {system or platform.platform()}"
    )


def build_gui_service(
    config: ExecutorConfig,
    store: ToolSessionStore,
    backend: GuiBackend | None = None,
) -> GuiService:
    """Compose the generic GUI service with the executor platform backend."""
    return GuiService(
        config,
        store,
        backend,
        backend_factory=_backend_for_platform,
    )


@lru_cache(maxsize=8)
def _linux_gui_capability_available(
    display: str | None,
    wayland_display: str | None,
    session_type: str | None,
) -> bool:
    resolved_type = (session_type or "").lower()
    if not resolved_type:
        if wayland_display:
            resolved_type = "wayland"
        elif display:
            resolved_type = "x11"
        else:
            return False
    if resolved_type == "x11":
        if not display:
            return False
    elif resolved_type == "wayland":
        if not wayland_display:
            return False
    else:
        return False

    candidates = [sys.executable, "/usr/bin/python3", shutil.which("python3")]
    for candidate in dict.fromkeys(item for item in candidates if item):
        if not os.path.isfile(candidate):
            continue
        try:
            result = subprocess.run(
                [
                    candidate,
                    "-c",
                    (
                        "import gi; gi.require_version('Atspi','2.0'); "
                        "from gi.repository import Atspi"
                    ),
                ],
                capture_output=True,
                timeout=3,
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            continue
        if result.returncode != 0:
            continue
        if resolved_type == "x11":
            return importlib.util.find_spec("Xlib") is not None
        return importlib.util.find_spec("dbus_next") is not None
    return False


def _macos_gui_capability_available() -> bool:
    return (
        importlib.util.find_spec("ApplicationServices") is not None
        and importlib.util.find_spec("Quartz") is not None
    )


def _linux_desktop_environment_fallback() -> tuple[
    str | None, str | None, str | None
]:
    from .linux import _desktop_environment

    env = _desktop_environment()
    return (
        env.get("DISPLAY"),
        env.get("WAYLAND_DISPLAY"),
        env.get("XDG_SESSION_TYPE"),
    )


def gui_capability_available() -> bool:
    """Return whether this executor can expose the native desktop GUI backend."""
    system = platform.system()
    if system == "Darwin":
        return _macos_gui_capability_available()
    if system != "Linux":
        return False
    display = os.environ.get("DISPLAY")
    wayland_display = os.environ.get("WAYLAND_DISPLAY")
    session_type = os.environ.get("XDG_SESSION_TYPE")
    resolved_type = (session_type or "").lower()
    needs_fallback = (
        (resolved_type == "x11" and not display)
        or (resolved_type == "wayland" and not wayland_display)
        or (
            resolved_type not in {"x11", "wayland"}
            and not (display or wayland_display)
        )
    )
    if needs_fallback:
        fallback_display, fallback_wayland, fallback_type = (
            _linux_desktop_environment_fallback()
        )
        display = display or fallback_display
        wayland_display = wayland_display or fallback_wayland
        session_type = session_type or fallback_type
    return _linux_gui_capability_available(
        display,
        wayland_display,
        session_type,
    )


__all__ = ["GuiService", "build_gui_service", "gui_capability_available"]
