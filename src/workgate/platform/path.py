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


def archive_member_key(name: str, *, system: str | None = None) -> str:
    """Validate one archive member against the destination host's path rules."""
    if host_os(system) is not HostOS.WINDOWS:
        return str(Path(name))
    member = PureWindowsPath(name)
    if (
        member.drive
        or member.root
        or not member.parts
        or any(
            part in {".", ".."} or ntpath.isreserved(part)
            for part in member.parts
        )
    ):
        raise ValueError(f"unsafe Windows archive member path: {name}")
    # Windows accepts both slash kinds, and most default volumes are
    # case-insensitive; reject colliding archive members before extraction.
    return ntpath.normcase(str(member))
