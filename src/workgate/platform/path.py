"""Parse executor-supplied path *names* without using the Control host OS."""

import ntpath
from pathlib import Path, PurePosixPath, PureWindowsPath

from .host import HostOS, host_os


def remote_path_name(path: str) -> str:
    """Return one basename for either native path syntax, without opening it."""
    if path.startswith("/"):
        return PurePosixPath(path).name
    if "\\" in path or (
        len(path) >= 2 and path[1] == ":" and path[0].isalpha()
    ):
        return PureWindowsPath(path).name
    return PurePosixPath(path).name


def path_lock_key(path: Path, *, system: str | None = None) -> str:
    """Map native case-insensitive aliases to one filesystem lock key."""
    raw = str(path)
    return (
        ntpath.normcase(ntpath.normpath(raw))
        if host_os(system) is HostOS.WINDOWS
        else raw
    )
