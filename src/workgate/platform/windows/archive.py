"""Native handle-relative, bounded Windows archive packing."""

import ctypes
import os
import tarfile
from contextlib import ExitStack
from ctypes import wintypes
from pathlib import Path
from typing import Any, cast

from .nt_relative import (
    _api,
    _assert_directory,
    close_handle,
    open_child,
    pinned_parent,
)
from .security import _verify_regular_handle

_DIRECTORY_NAMES_INFO = 10
_ERROR_NO_MORE_FILES = 18
_READ_ATTRIBUTES = 0x80
_LIST_DIRECTORY = 0x1
_GENERIC_READ = 0x80000000


class _IdBothDirectoryInfo(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", wintypes.DWORD),
        ("FileIndex", wintypes.DWORD),
        ("CreationTime", ctypes.c_int64),
        ("LastAccessTime", ctypes.c_int64),
        ("LastWriteTime", ctypes.c_int64),
        ("ChangeTime", ctypes.c_int64),
        ("EndOfFile", ctypes.c_int64),
        ("AllocationSize", ctypes.c_int64),
        ("FileAttributes", wintypes.DWORD),
        ("FileNameLength", wintypes.DWORD),
        ("EaSize", wintypes.DWORD),
        ("ShortNameLength", ctypes.c_byte),
        ("ShortName", wintypes.WCHAR * 12),
        ("FileId", ctypes.c_int64),
        ("FileName", wintypes.WCHAR * 1),
    ]


def _children(kernel: Any, handle: int) -> list[str]:
    """Read native FileIdBothDirectoryInfo entries from a directory handle."""
    list_names = _api(
        kernel,
        "GetFileInformationByHandleEx",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    entries: list[str] = []
    buffer = ctypes.create_string_buffer(65536)
    while True:
        if not list_names(
            handle, _DIRECTORY_NAMES_INFO, buffer, ctypes.sizeof(buffer)
        ):
            code = cast(Any, ctypes).get_last_error()
            if code == _ERROR_NO_MORE_FILES:
                break
            raise OSError(code, cast(Any, ctypes).FormatError(code))
        offset = 0
        while True:
            if offset + _IdBothDirectoryInfo.FileName.offset > len(buffer):
                raise OSError("Malformed Windows directory enumeration")
            item = _IdBothDirectoryInfo.from_buffer(buffer, offset)
            next_offset = item.NextEntryOffset
            name_length = item.FileNameLength
            if (
                name_length % 2
                or not name_length
                or name_length
                > len(buffer) - offset - _IdBothDirectoryInfo.FileName.offset
            ):
                raise OSError("Invalid native Windows directory entry name")
            base = offset + _IdBothDirectoryInfo.FileName.offset
            name = bytes(buffer.raw[base : base + name_length]).decode(
                "utf-16-le", errors="strict"
            )
            if name not in {".", ".."}:
                entries.append(name)
            if next_offset == 0:
                break
            if (
                next_offset % 4
                or next_offset < _IdBothDirectoryInfo.FileName.offset
            ):
                raise OSError("Malformed Windows directory entry offset")
            offset += next_offset
    return entries


def pack_directory(
    source: Path,
    archive: Path,
    *,
    compression: str,
    max_entries: int,
    max_bytes: int,
) -> None:
    """Pack one tree using NT handles. Refuse reparse and unsupported children."""
    import msvcrt

    kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    count = 0
    total = 0
    seen: set[str] = set()
    from .security import open_private_file

    mode = "w:gz" if compression == "gz" else "w"
    with pinned_parent(source) as parent:
        root = open_child(
            parent,
            source.name,
            access=_LIST_DIRECTORY | _READ_ATTRIBUTES,
            directory=True,
        )
        try:
            _assert_directory(kernel, root)
            with (
                os.fdopen(
                    open_private_file(archive, create=True, exclusive=True),
                    "wb",
                ) as archive_output,
                tarfile.open(fileobj=archive_output, mode=mode) as tar,
            ):

                def walk(directory_handle: int, prefix: str) -> None:
                    nonlocal count, total
                    for name in _children(kernel, directory_handle):
                        count += 1
                        if count > max_entries:
                            raise ValueError(
                                "Windows archive exceeds entry count limit"
                            )
                        key = (prefix + name).casefold()
                        if key in seen:
                            raise ValueError(
                                "Windows archive has colliding names"
                            )
                        seen.add(key)
                        with ExitStack() as stack:
                            try:
                                child = open_child(
                                    directory_handle,
                                    name,
                                    access=_LIST_DIRECTORY | _READ_ATTRIBUTES,
                                    directory=True,
                                )
                            except NotADirectoryError:
                                child = open_child(
                                    directory_handle,
                                    name,
                                    access=_GENERIC_READ | _READ_ATTRIBUTES,
                                    directory=False,
                                )
                                kind = "file"
                            else:
                                kind = "dir"
                            stack.callback(close_handle, kernel, child)
                            path = prefix + name
                            if kind == "dir":
                                _assert_directory(kernel, child)
                                info = tarfile.TarInfo(path + "/")
                                info.type = tarfile.DIRTYPE
                                info.mode = 0o755
                                tar.addfile(info)
                                walk(child, path + "/")
                            else:
                                _verify_regular_handle(kernel, child)
                                fd = cast(Any, msvcrt).open_osfhandle(
                                    child, os.O_RDONLY | cast(Any, os).O_BINARY
                                )
                                stack.pop_all()  # fd owns child handle now
                                with os.fdopen(fd, "rb") as content:
                                    info = tarfile.TarInfo(path)
                                    info.size = os.fstat(
                                        content.fileno()
                                    ).st_size
                                    info.mode = 0o644
                                    total += info.size
                                    if total > max_bytes:
                                        raise ValueError(
                                            "Windows archive exceeds bytes limit"
                                        )
                                    tar.addfile(info, content)

                walk(root, "")
        finally:
            close_handle(kernel, root)


def _open_directory(
    kernel: Any,
    parent: int,
    name: str,
    *,
    delete: bool = False,
    read_control: bool = False,
) -> int:
    """Open a directory without following its final reparse point."""
    handle = open_child(
        parent,
        name,
        access=_LIST_DIRECTORY
        | _READ_ATTRIBUTES
        | (0x10000 if delete else 0)
        | (0x20000 if read_control else 0),
        directory=True,
    )
    try:
        _assert_directory(kernel, handle)
        return handle
    except BaseException:
        close_handle(kernel, handle)
        raise


def move_directory(
    source: Path, destination: Path, *, require_private_source: bool = False
) -> None:
    """Rename a native directory; additionally verify owner+DACL for staging."""
    from .nt_relative import rename_child

    kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    with (
        pinned_parent(source) as source_parent,
        pinned_parent(destination) as destination_parent,
    ):
        handle = _open_directory(
            kernel,
            source_parent,
            source.name,
            delete=True,
            read_control=require_private_source,
        )
        try:
            if require_private_source:
                from .security import _libs, _sid, _verify

                _, advapi = _libs()
                _verify(
                    kernel, advapi, handle, _sid(kernel, advapi), directory=True
                )
            # A directory target must be absent. The caller manages any backup.
            try:
                occupied = open_child(
                    destination_parent,
                    destination.name,
                    access=_READ_ATTRIBUTES,
                    directory=True,
                )
            except FileNotFoundError:
                pass
            else:
                close_handle(kernel, occupied)
                raise FileExistsError(str(destination))
            rename_child(handle, destination_parent, destination.name)
        finally:
            close_handle(kernel, handle)


def remove_directory_tree(
    path: Path, *, require_private_root: bool = False
) -> None:
    """Delete a native tree; optionally require WorkGate owner/DACL at root."""
    from .nt_relative import delete_child

    kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    with pinned_parent(path) as parent:
        root = _open_directory(
            kernel,
            parent,
            path.name,
            delete=True,
            read_control=require_private_root,
        )
        try:
            if require_private_root:
                from .security import _libs, _sid, _verify

                _, advapi = _libs()
                _verify(
                    kernel, advapi, root, _sid(kernel, advapi), directory=True
                )

            def remove_children(directory: int) -> None:
                for name in _children(kernel, directory):
                    try:
                        child = _open_directory(
                            kernel, directory, name, delete=True
                        )
                    except NotADirectoryError:
                        child = open_child(
                            directory,
                            name,
                            access=_READ_ATTRIBUTES | 0x10000,
                            directory=False,
                        )
                        try:
                            _verify_regular_handle(kernel, child)
                            delete_child(child)
                        finally:
                            close_handle(kernel, child)
                    else:
                        try:
                            remove_children(child)
                            delete_child(child)
                        finally:
                            close_handle(kernel, child)

            remove_children(root)
            delete_child(root)
        finally:
            close_handle(kernel, root)


def _validated_members(
    tar: tarfile.TarFile, *, max_entries: int, max_bytes: int
) -> list[tarfile.TarInfo]:
    """Reject unsafe metadata before allowing any filesystem mutation."""
    from ..path import archive_member_key

    members: list[tarfile.TarInfo] = []
    seen: set[str] = set()
    total_bytes = 0
    for member in tar:
        if len(members) >= max_entries:
            raise ValueError("archive exceeds entries limit")
        key = archive_member_key(member.name, system="Windows")
        if key in seen:
            raise ValueError("duplicate Windows archive member")
        seen.add(key)
        if not (member.isdir() or member.isfile()) or getattr(
            member, "sparse", None
        ):
            raise ValueError("unsupported Windows archive member type")
        if member.isfile():
            total_bytes += max(0, int(member.size))
            if total_bytes > max_bytes:
                raise ValueError("archive exceeds unpacked byte limit")
        members.append(member)
    return members


def inspect_archive(
    archive: Path, *, max_entries: int, max_bytes: int
) -> tuple[int, str]:
    """Validate archive metadata and capture immutable receipt identity."""
    import hashlib

    from .security import open_regular_file_no_follow

    fd = open_regular_file_no_follow(archive, share_write=False)
    with os.fdopen(fd, "rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r:*") as tar:
            count = len(
                _validated_members(
                    tar, max_entries=max_entries, max_bytes=max_bytes
                )
            )
    return count, digest


def extract_archive(
    archive: Path,
    staging: Path,
    *,
    max_entries: int,
    max_bytes: int,
    expected_bytes: int | None = None,
    expected_sha256: str | None = None,
) -> int:
    """Validate all tar entries, then extract via protected no-follow handles."""
    import hashlib
    import shutil
    from pathlib import PureWindowsPath

    from .security import (
        ensure_private_directory,
        open_private_file,
        open_regular_file_no_follow,
    )

    entries: list[tarfile.TarInfo] = []
    archive_fd = open_regular_file_no_follow(archive, share_write=False)
    with os.fdopen(archive_fd, "rb") as stream:
        if (
            expected_bytes is not None
            and os.fstat(stream.fileno()).st_size != expected_bytes
        ):
            raise ValueError("transfer archive size changed before unpack")
        if expected_sha256 is not None:
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(65536), b""):
                digest.update(block)
            if digest.hexdigest() != expected_sha256:
                raise ValueError(
                    "transfer archive sha256 changed before unpack"
                )
            stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r:*") as tar:
            entries = _validated_members(
                tar, max_entries=max_entries, max_bytes=max_bytes
            )
            # Do not touch staging until all untrusted metadata is validated.
            ensure_private_directory(staging)
            for member in entries:
                parts = PureWindowsPath(member.name).parts
                target = staging.joinpath(*parts)
                if member.isdir():
                    ensure_private_directory(target)
                    continue
                ensure_private_directory(target.parent)
                source = tar.extractfile(member)
                if source is None:
                    raise ValueError("archive member has no content")
                with (
                    source,
                    os.fdopen(
                        open_private_file(target, create=True, exclusive=True),
                        "wb",
                    ) as output,
                ):
                    shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
    return len(entries)


def is_empty_directory(path: Path) -> bool:
    """Inspect the directory object itself, never a possibly swapped path."""
    kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    with pinned_parent(path) as parent:
        handle = _open_directory(kernel, parent, path.name)
        try:
            return not _children(kernel, handle)
        finally:
            close_handle(kernel, handle)


def verify_directory(path: Path, *, require_private: bool = False) -> None:
    """Check a directory handle and optionally its WorkGate-owned ACL identity."""
    kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    with pinned_parent(path) as parent:
        handle = _open_directory(
            kernel, parent, path.name, read_control=require_private
        )
        try:
            if require_private:
                from .security import _libs, _sid, _verify

                _, advapi = _libs()
                _verify(
                    kernel, advapi, handle, _sid(kernel, advapi), directory=True
                )
        finally:
            close_handle(kernel, handle)
