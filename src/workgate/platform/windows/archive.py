"""Native handle-relative, bounded Windows archive packing."""

import ctypes
import os
import tarfile
from collections.abc import Iterator
from contextlib import ExitStack, suppress
from ctypes import wintypes
from pathlib import Path
from typing import Any, BinaryIO, cast

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


def _children(kernel: Any, handle: int) -> Iterator[str]:
    """Yield names from a verified directory handle without unbounded buffering."""
    list_names = _api(
        kernel,
        "GetFileInformationByHandleEx",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
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
                yield name
            if next_offset == 0:
                break
            if (
                next_offset % 4
                or next_offset < _IdBothDirectoryInfo.FileName.offset
            ):
                raise OSError("Malformed Windows directory entry offset")
            offset += next_offset


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
            rename_child(
                handle, destination_parent, destination.name, replace=False
            )
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

            def remove_link(directory: int, name: str) -> None:
                from .security import _open_reparse_entry

                handle = _open_reparse_entry(kernel, directory, name)
                try:
                    delete_child(handle)
                finally:
                    close_handle(kernel, handle)

            def remove_children(directory: int) -> None:
                for name in _children(kernel, directory):
                    try:
                        child = _open_directory(
                            kernel, directory, name, delete=True
                        )
                    except PermissionError:
                        # The opened child was a reparse directory (or has
                        # insufficient permissions). Reopen only its own
                        # metadata and refuse if it is not really a reparse.
                        remove_link(directory, name)
                    except NotADirectoryError:
                        child = open_child(
                            directory,
                            name,
                            access=_READ_ATTRIBUTES | 0x10000,
                            directory=False,
                        )
                        try:
                            _verify_regular_handle(kernel, child)
                        except PermissionError:
                            close_handle(kernel, child)
                            remove_link(directory, name)
                        else:
                            try:
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
            return next(_children(kernel, handle), None) is None
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


class _WinFileTime(ctypes.Structure):
    _fields_ = [
        ("low", wintypes.DWORD),
        ("high", wintypes.DWORD),
    ]


class _HandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("created", _WinFileTime),
        ("accessed", _WinFileTime),
        ("modified", _WinFileTime),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


def list_tree(
    path: Path, *, recursive: bool, max_entries: int
) -> tuple[list[tuple[Path, str, int | None, float]], bool]:
    """List native directory entries through verified handles; never follow links.

    Fail closed when a reparse entry is encountered rather than dereference it.
    Limit total entries and stream native enumeration to bound memory usage.
    """
    if max_entries < 0:
        raise ValueError("max_entries must be nonnegative")
    kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    info_fn = _api(
        kernel,
        "GetFileInformationByHandle",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.POINTER(_HandleFileInformation),
    )
    output: list[tuple[Path, str, int | None, float]] = []
    truncated = False

    def walk(handle: int, root: Path) -> None:
        nonlocal truncated
        for name in _children(kernel, handle):
            if len(output) >= max_entries:
                truncated = True
                return
            child_path = root / name
            try:
                child = open_child(
                    handle,
                    name,
                    access=_READ_ATTRIBUTES | _LIST_DIRECTORY,
                    directory=True,
                )
            except NotADirectoryError:
                child = open_child(
                    handle, name, access=_READ_ATTRIBUTES, directory=False
                )
            try:
                info = _HandleFileInformation()
                if not info_fn(child, ctypes.byref(info)):
                    code = cast(Any, ctypes).get_last_error()
                    raise OSError(code, cast(Any, ctypes).FormatError(code))
                if info.attributes & 0x400:
                    # Describe the link object only; never visit its target.
                    kind = "link"
                elif info.attributes & 0x10:
                    _assert_directory(kernel, child)
                    kind = "dir"
                else:
                    _verify_regular_handle(kernel, child)
                    kind = "file"
                timestamp = (
                    int(info.modified.high) << 32 | int(info.modified.low)
                ) / 10_000_000 - 11_644_473_600
                size = (
                    int(info.size_high) << 32 | int(info.size_low)
                    if kind in {"file", "link"}
                    else None
                )
                output.append((child_path, kind, size, timestamp))
                if recursive and kind == "dir":
                    walk(child, child_path)
                    if truncated:
                        return
            finally:
                close_handle(kernel, child)

    with pinned_parent(path) as parent:
        root = _open_directory(kernel, parent, path.name)
        try:
            walk(root, path)
        finally:
            close_handle(kernel, root)
    return output, truncated


def _copy_exact_file(
    source: BinaryIO, destination: BinaryIO, size: int
) -> None:
    """Copy at most one pinned source size, rejecting growth and truncation."""
    remaining = size
    while remaining:
        chunk = source.read(min(remaining, 1024 * 1024))
        if not chunk:
            raise RuntimeError("Windows directory copy source was truncated")
        destination.write(chunk)
        remaining -= len(chunk)
    if source.read(1):
        raise RuntimeError("Windows directory copy source grew during copy")


def copy_directory(
    source: Path,
    destination: Path,
    *,
    max_entries: int,
    max_bytes: int,
) -> None:
    """Copy a native Windows tree into private staging, then publish atomically.

    A directory remains owner-private until its complete copied tree is
    published. Symlinks, junctions, special entries and changed files fail
    closed; source security descriptors are copied through verified handles.
    """
    import msvcrt
    import secrets

    from .security import (
        _verify_regular_handle,
        copy_handle_owner_dacl,
        ensure_private_directory,
        open_private_file,
    )

    if max_entries < 1 or max_bytes < 0:
        raise ValueError("Invalid Windows directory copy limits")
    try:
        destination.relative_to(source)
    except ValueError:
        pass
    else:
        raise ValueError("A directory cannot be copied inside itself")

    kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    staged_path = destination.with_name(
        f".{destination.name}.copy-{secrets.token_hex(12)}"
    )
    count = 0
    total = 0
    with pinned_parent(source) as source_parent:
        original = _open_directory(
            kernel, source_parent, source.name, read_control=True
        )
        try:
            ensure_private_directory(staged_path)
            try:
                with pinned_parent(staged_path) as stage_parent:
                    stage_root = open_child(
                        stage_parent,
                        staged_path.name,
                        access=_LIST_DIRECTORY
                        | _READ_ATTRIBUTES
                        | 0x20000
                        | 0x40000
                        | 0x80000,
                        directory=True,
                    )
                    try:
                        _assert_directory(kernel, stage_root)

                        def walk(
                            source_handle: int,
                            target_handle: int,
                            target_path: Path,
                        ) -> None:
                            nonlocal count, total
                            for name in _children(kernel, source_handle):
                                count += 1
                                if count > max_entries:
                                    raise ValueError(
                                        "Windows directory copy exceeds entry limit"
                                    )
                                target_child_path = target_path / name
                                try:
                                    child = _open_directory(
                                        kernel,
                                        source_handle,
                                        name,
                                        read_control=True,
                                    )
                                except NotADirectoryError:
                                    child = open_child(
                                        source_handle,
                                        name,
                                        access=_GENERIC_READ | 0x20000,
                                        directory=False,
                                        share_access=5,
                                    )
                                    try:
                                        _verify_regular_handle(kernel, child)
                                    except BaseException:
                                        close_handle(kernel, child)
                                        raise
                                    try:
                                        src_fd = cast(
                                            Any, msvcrt
                                        ).open_osfhandle(
                                            child,
                                            os.O_RDONLY
                                            | cast(Any, os).O_BINARY,
                                        )
                                    except BaseException:
                                        close_handle(kernel, child)
                                        raise
                                    with os.fdopen(src_fd, "rb") as reader:
                                        before = os.fstat(reader.fileno())
                                        total += before.st_size
                                        if total > max_bytes:
                                            raise ValueError(
                                                "Windows directory copy exceeds byte limit"
                                            ) from None
                                        with os.fdopen(
                                            open_private_file(
                                                target_child_path,
                                                create=True,
                                                exclusive=True,
                                                write_security=True,
                                            ),
                                            "wb",
                                        ) as writer:
                                            _copy_exact_file(
                                                reader, writer, before.st_size
                                            )
                                            writer.flush()
                                            os.fsync(writer.fileno())
                                            copy_handle_owner_dacl(
                                                cast(Any, msvcrt).get_osfhandle(
                                                    reader.fileno()
                                                ),
                                                cast(Any, msvcrt).get_osfhandle(
                                                    writer.fileno()
                                                ),
                                            )
                                        after = os.fstat(reader.fileno())
                                        if (
                                            before.st_size,
                                            before.st_mtime_ns,
                                        ) != (after.st_size, after.st_mtime_ns):
                                            raise RuntimeError(
                                                "Windows directory copy source changed"
                                            ) from None
                                else:
                                    try:
                                        ensure_private_directory(
                                            target_child_path
                                        )
                                        copied = open_child(
                                            target_handle,
                                            name,
                                            access=_LIST_DIRECTORY
                                            | _READ_ATTRIBUTES
                                            | 0x20000
                                            | 0x40000
                                            | 0x80000,
                                            directory=True,
                                        )
                                        try:
                                            _assert_directory(kernel, copied)
                                            walk(
                                                child, copied, target_child_path
                                            )
                                            copy_handle_owner_dacl(
                                                child, copied
                                            )
                                        finally:
                                            close_handle(kernel, copied)
                                    finally:
                                        close_handle(kernel, child)

                        walk(original, stage_root, staged_path)
                        # Finalize *all* security metadata before publication,
                        # so no ACL failure can leave a partially protected
                        # destination visible to the user.
                        copy_handle_owner_dacl(original, stage_root)
                        move_directory(staged_path, destination)
                    finally:
                        close_handle(kernel, stage_root)
            except BaseException:
                # Only ever delete our private, unpredictable staging name:
                # a failed no-clobber rename must not touch the user target.
                with suppress(OSError):
                    remove_directory_tree(staged_path)
                raise
        finally:
            close_handle(kernel, original)
