"""NT directory-handle-relative file operations; never re-resolve a child path.

The Win32 CreateFileW APIs accept absolute paths, which can be redirected
between parent validation and opening. NtCreateFile RootDirectory instead
anchors every descendant open to an already-verified directory object.
"""

import contextlib
import ctypes
import ntpath
from ctypes import wintypes
from pathlib import Path
from typing import Any, cast

_ATTR_TAG = 9
_DIRECTORY = 0x10
_REPARSE = 0x400
_FILE_READ_ATTRIBUTES = 0x80
_SYNCHRONIZE = 0x00100000
_SHARE_ALL = 7
_OPEN = 1
_CREATE = 2
_OPEN_IF = 3
_DIRECTORY_FILE = 0x1
_NON_DIRECTORY_FILE = 0x40
_SYNCHRONOUS_IO = 0x20
_OPEN_REPARSE = 0x00200000
_OBJ_CASE_INSENSITIVE = 0x40
_OBJ_DONT_REPARSE = 0x1000
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", wintypes.LPVOID),
        ("SecurityQualityOfService", wintypes.LPVOID),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_void_p)]


class _AttributeTag(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", wintypes.DWORD),
        ("ReparseTag", wintypes.DWORD),
    ]


def _api(dll: Any, name: str, restype: Any, *argtypes: Any) -> Any:
    func = getattr(dll, name)
    func.restype = restype
    func.argtypes = argtypes
    return func


def _nt_error(status: int) -> None:
    ntdll = cast(Any, ctypes).WinDLL("ntdll", use_last_error=True)
    to_winerror = _api(
        ntdll, "RtlNtStatusToDosError", wintypes.ULONG, ctypes.c_long
    )
    error = int(to_winerror(status))
    detail = f"Windows NtCreateFile failure 0x{status & 0xFFFFFFFF:08x}"
    if error in {80, 183}:
        raise FileExistsError(error, detail)
    if error in {2, 3}:
        raise FileNotFoundError(error, detail)
    if error in {5, 32}:
        raise PermissionError(error, detail)
    raise OSError(error, detail)


def open_child(
    root_handle: int,
    name: str,
    *,
    access: int,
    directory: bool,
    create: bool = False,
    exclusive: bool = False,
    security_descriptor: int | None = None,
) -> int:
    """Open/create one child by name relative to the pinned directory handle."""
    if (
        not name
        or name in {".", ".."}
        or "\\" in name
        or "/" in name
        or ":" in name
        or ntpath.isreserved(name)
    ):
        raise ValueError("Windows native child name is unsafe")
    buffer = ctypes.create_unicode_buffer(name)
    name_bytes = len(name.encode("utf-16-le"))
    if name_bytes > 0xFFFC:
        raise ValueError("Windows native child name is too long")
    unicode_name = _UnicodeString(
        name_bytes, name_bytes + 2, ctypes.cast(buffer, wintypes.LPWSTR)
    )
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        root_handle,
        ctypes.pointer(unicode_name),
        _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE,
        security_descriptor,
        None,
    )
    status = _IoStatusBlock()
    handle = wintypes.HANDLE()
    ntdll = cast(Any, ctypes).WinDLL("ntdll", use_last_error=True)
    nt_create = _api(
        ntdll,
        "NtCreateFile",
        ctypes.c_long,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
    )
    disposition = _CREATE if exclusive else (_OPEN_IF if create else _OPEN)
    options = (
        (_DIRECTORY_FILE if directory else _NON_DIRECTORY_FILE)
        | _SYNCHRONOUS_IO
        | _OPEN_REPARSE
    )
    result = int(
        nt_create(
            ctypes.byref(handle),
            access | _SYNCHRONIZE,
            ctypes.byref(attributes),
            ctypes.byref(status),
            None,
            0,
            _SHARE_ALL,
            disposition,
            options,
            None,
            0,
        )
    )
    if result < 0:
        _nt_error(result)
    if handle.value is None:
        raise OSError("NtCreateFile succeeded without a handle")
    return int(handle.value)


def close_handle(kernel: Any, handle: int) -> None:
    _api(kernel, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)(handle)


def _assert_directory(kernel: Any, handle: int) -> None:
    get_tag = _api(
        kernel,
        "GetFileInformationByHandleEx",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    tag = _AttributeTag()
    if not get_tag(handle, _ATTR_TAG, ctypes.byref(tag), ctypes.sizeof(tag)):
        code = cast(Any, ctypes).get_last_error()
        raise OSError(code, cast(Any, ctypes).FormatError(code))
    if tag.FileAttributes & _REPARSE:
        raise PermissionError(
            "Windows parent directory is a junction/reparse point"
        )
    if not tag.FileAttributes & _DIRECTORY:
        raise NotADirectoryError("Windows parent is not a directory")


@contextlib.contextmanager
def pinned_parent(path: Path):
    """Walk from the volume root through handles, not absolute child strings."""
    if not path.is_absolute():
        raise ValueError("Windows secure path must be absolute")
    kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    create_file = _api(
        kernel,
        "CreateFileW",
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    with contextlib.ExitStack() as stack:
        root = path.anchor
        handle = create_file(
            root,
            _FILE_READ_ATTRIBUTES,
            _SHARE_ALL,
            None,
            3,
            0x02000000 | _OPEN_REPARSE,
            None,
        )
        if handle == _INVALID_HANDLE:
            code = cast(Any, ctypes).get_last_error()
            raise OSError(code, cast(Any, ctypes).FormatError(code))
        stack.callback(close_handle, kernel, int(handle))
        _assert_directory(kernel, int(handle))
        for part in path.parent.relative_to(Path(root)).parts:
            handle = open_child(
                int(handle),
                part,
                access=_FILE_READ_ATTRIBUTES,
                directory=True,
            )
            stack.callback(close_handle, kernel, int(handle))
            _assert_directory(kernel, int(handle))
        yield int(handle)


class _FileRenameInfo(ctypes.Structure):
    """Native FILE_RENAME_INFORMATION for NtSetInformationFile class 10."""

    _fields_ = [
        ("ReplaceIfExists", wintypes.BOOLEAN),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.ULONG),
        ("FileName", wintypes.WCHAR * 1),
    ]


def rename_child(source: int, destination_parent: int, name: str) -> None:
    """Rename an open source handle into a verified directory handle."""
    if (
        not name
        or name in {".", ".."}
        or any(c in name for c in "\\/:")
        or ntpath.isreserved(name)
    ):
        raise ValueError("Unsafe Windows rename destination")
    encoded = name.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(
        _FileRenameInfo.FileName.offset + len(encoded)
    )
    info = _FileRenameInfo.from_buffer(buffer)
    info.ReplaceIfExists = 1
    info.RootDirectory = destination_parent
    info.FileNameLength = len(encoded)
    ctypes.memmove(
        ctypes.addressof(buffer) + _FileRenameInfo.FileName.offset,
        encoded,
        len(encoded),
    )
    status = _IoStatusBlock()
    ntdll = cast(Any, ctypes).WinDLL("ntdll", use_last_error=True)
    set_information = _api(
        ntdll,
        "NtSetInformationFile",
        ctypes.c_long,
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    )
    result = int(
        set_information(source, ctypes.byref(status), buffer, len(buffer), 10)
    )
    if result < 0:
        _nt_error(result)


class _FileDispositionInfo(ctypes.Structure):
    _fields_ = [("DeleteFile", wintypes.BOOLEAN)]


def delete_child(handle: int) -> None:
    """Mark a verified open NT file handle for deletion on close."""
    ntdll = cast(Any, ctypes).WinDLL("ntdll", use_last_error=True)
    set_info = _api(
        ntdll,
        "NtSetInformationFile",
        ctypes.c_long,
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    )
    information = _FileDispositionInfo(True)
    status = _IoStatusBlock()
    result = int(
        set_info(
            handle,
            ctypes.byref(status),
            ctypes.byref(information),
            ctypes.sizeof(information),
            13,
        )
    )
    if result < 0:
        _nt_error(result)
