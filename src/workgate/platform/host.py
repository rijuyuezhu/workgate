"""Centralize supported native host identification without guessing capabilities."""

import platform
from enum import StrEnum


class HostOS(StrEnum):
    LINUX = "Linux"
    MACOS = "Darwin"
    WINDOWS = "Windows"


def host_os(system: str | None = None) -> HostOS | None:
    """Identify a supported native host; unknown hosts are never assumed POSIX."""
    value = platform.system() if system is None else system
    try:
        return HostOS(value)
    except ValueError:
        return None
