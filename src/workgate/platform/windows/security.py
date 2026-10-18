"""Windows native ACL creation and handle verification (stdlib only)."""

import contextlib
import ctypes
import os
import re
from ctypes import wintypes
from pathlib import Path
from typing import Any, cast

from .nt_relative import (
    close_handle,
    delete_child,
    open_child,
    pinned_parent,
    rename_child,
)

_READ_CONTROL = 0x20000
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_SHARE_ALL = 7
_DIRECTORY = 0x10
_REPARSE = 0x400
_ATTR_TAG_INFO = 9


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _AttributeTag(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", wintypes.DWORD),
        ("ReparseTag", wintypes.DWORD),
    ]


def _libs():
    loader = cast(Any, ctypes).WinDLL
    return loader("kernel32", use_last_error=True), loader(
        "advapi32", use_last_error=True
    )


def _api(dll: Any, name: str, restype: Any, *args: Any) -> Any:
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = args
    return fn


def _fail() -> None:
    code = cast(Any, ctypes).get_last_error()
    raise OSError(code, cast(Any, ctypes).FormatError(code))


def _sid(kernel: Any, advapi: Any) -> str:
    get_process = _api(kernel, "GetCurrentProcess", wintypes.HANDLE)
    open_token = _api(
        advapi,
        "OpenProcessToken",
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    get_token = _api(
        advapi,
        "GetTokenInformation",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    to_string = _api(
        advapi,
        "ConvertSidToStringSidW",
        wintypes.BOOL,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    )
    close = _api(kernel, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    free = _api(kernel, "LocalFree", wintypes.HANDLE, wintypes.HANDLE)
    token = wintypes.HANDLE()
    if not open_token(get_process(), 0x8, ctypes.byref(token)):
        _fail()
    try:
        size = wintypes.DWORD()
        get_token(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not get_token(token, 1, buffer, size, ctypes.byref(size)):
            _fail()
        sid_ptr = ctypes.c_void_p.from_buffer(buffer).value
        out = wintypes.LPWSTR()
        if not to_string(sid_ptr, ctypes.byref(out)):
            _fail()
        try:
            return out.value or ""
        finally:
            free(ctypes.cast(out, wintypes.HANDLE))
    finally:
        close(token)


@contextlib.contextmanager
def _descriptor(kernel: Any, advapi: Any, user_sid: str):
    convert = _api(
        advapi,
        "ConvertStringSecurityDescriptorToSecurityDescriptorW",
        wintypes.BOOL,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    )
    free = _api(kernel, "LocalFree", wintypes.HANDLE, wintypes.HANDLE)
    pointer = wintypes.LPVOID()
    if not convert(
        f"O:{user_sid}D:P(A;;FA;;;{user_sid})(A;;FA;;;SY)",
        1,
        ctypes.byref(pointer),
        None,
    ):
        _fail()
    attr = _SecurityAttributes(
        ctypes.sizeof(_SecurityAttributes), pointer, False
    )
    try:
        yield attr
    finally:
        free(ctypes.cast(pointer, wintypes.HANDLE))


def _verify(
    kernel: Any, advapi: Any, handle: int, user_sid: str, *, directory: bool
):
    tag = _AttributeTag()
    get_tag = _api(
        kernel,
        "GetFileInformationByHandleEx",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    if not get_tag(
        handle, _ATTR_TAG_INFO, ctypes.byref(tag), ctypes.sizeof(tag)
    ):
        _fail()
    if tag.FileAttributes & _REPARSE:
        raise PermissionError(
            "Refusing Windows private reparse point / junction"
        )
    if bool(tag.FileAttributes & _DIRECTORY) != directory:
        raise PermissionError("Wrong Windows private handle type")
    get_security = _api(
        advapi,
        "GetSecurityInfo",
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
    )
    stringify = _api(
        advapi,
        "ConvertSecurityDescriptorToStringSecurityDescriptorW",
        wintypes.BOOL,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    )
    free = _api(kernel, "LocalFree", wintypes.HANDLE, wintypes.HANDLE)
    owner = wintypes.LPVOID()
    sd = wintypes.LPVOID()
    code = get_security(
        handle, 1, 5, ctypes.byref(owner), None, None, None, ctypes.byref(sd)
    )
    if code:
        raise OSError(code, cast(Any, ctypes).FormatError(code))
    try:
        sid_to_string = _api(
            advapi,
            "ConvertSidToStringSidW",
            wintypes.BOOL,
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.LPWSTR),
        )
        owner_text = wintypes.LPWSTR()
        if not sid_to_string(owner, ctypes.byref(owner_text)):
            _fail()
        try:
            if owner_text.value != user_sid:
                raise PermissionError(
                    "Windows private file owner differs from current user"
                )
        finally:
            free(ctypes.cast(owner_text, wintypes.HANDLE))
        rendered = wintypes.LPWSTR()
        if not stringify(sd, 1, 4, ctypes.byref(rendered), None):
            _fail()
        try:
            dacl = rendered.value or ""
        finally:
            free(ctypes.cast(rendered, wintypes.HANDLE))
        if not dacl.startswith("D:P"):
            raise PermissionError("Windows private DACL must be protected")
        aces = re.findall(r"\(([^()]*)\)", dacl[3:])
        valid = {
            ("A", "", "FA", "", "", user_sid),
            ("A", "", "FA", "", "", "SY"),
        }
        if len(aces) != 2 or {tuple(a.split(";")) for a in aces} != valid:
            raise PermissionError(
                f"Windows private DACL has unexpected ACEs: {dacl}"
            )
    finally:
        free(ctypes.cast(sd, wintypes.HANDLE))


def _open(
    kernel: Any,
    advapi: Any,
    root_handle: int,
    name: str,
    user_sid: str,
    *,
    directory: bool,
    create: bool = False,
    exclusive: bool = False,
    attributes: _SecurityAttributes | None = None,
    extra_access: int = 0,
    append: bool = False,
) -> int:
    """Open a child relative to a verified parent handle, not a string path."""
    handle = open_child(
        root_handle,
        name,
        access=_READ_CONTROL
        | 0x80
        | extra_access
        | (
            0
            if directory
            else (0x4 if append else _GENERIC_READ | _GENERIC_WRITE)
        ),
        directory=directory,
        create=create,
        exclusive=exclusive,
        security_descriptor=(
            attributes.lpSecurityDescriptor if attributes is not None else None
        ),
    )
    try:
        _verify(kernel, advapi, handle, user_sid, directory=directory)
        return handle
    except BaseException:
        close_handle(kernel, handle)
        raise


def _prepare_parent(path: Path) -> None:
    """Create missing parent components through verified native handles."""
    if not path.parent.exists():
        ensure_private_directory(path.parent)


def ensure_private_directory(path: Path) -> Path:
    _prepare_parent(path)
    kernel, advapi = _libs()
    sid = _sid(kernel, advapi)
    with (
        pinned_parent(path) as parent,
        _descriptor(kernel, advapi, sid) as security,
    ):
        handle = _open(
            kernel,
            advapi,
            parent,
            path.name,
            sid,
            directory=True,
            create=True,
            attributes=security,
        )
        close_handle(kernel, handle)
    return path


def open_private_file(
    path: Path,
    *,
    create: bool,
    exclusive: bool = False,
    append: bool = False,
) -> int:
    import msvcrt

    kernel, advapi = _libs()
    sid = _sid(kernel, advapi)
    if create:
        _prepare_parent(path)
    with (
        pinned_parent(path) as parent,
        _descriptor(kernel, advapi, sid) as security,
    ):
        handle = _open(
            kernel,
            advapi,
            parent,
            path.name,
            sid,
            directory=False,
            create=create,
            exclusive=exclusive,
            attributes=security,
            append=append,
        )
    try:
        flags = (os.O_WRONLY | os.O_APPEND) if append else os.O_RDWR
        return cast(Any, msvcrt).open_osfhandle(
            handle, flags | cast(Any, os).O_BINARY
        )
    except BaseException:
        _api(kernel, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)(handle)
        raise


def replace_private_file(source: Path, destination: Path) -> None:
    """Atomically rename a DACL-verified file relative to pinned NT parents."""
    kernel, advapi = _libs()
    sid = _sid(kernel, advapi)
    with (
        pinned_parent(source) as source_root,
        pinned_parent(destination) as destination_root,
    ):
        source_handle = _open(
            kernel,
            advapi,
            source_root,
            source.name,
            sid,
            directory=False,
            extra_access=0x10000,
        )
        try:
            try:
                old_handle = _open(
                    kernel,
                    advapi,
                    destination_root,
                    destination.name,
                    sid,
                    directory=False,
                )
            except FileNotFoundError:
                pass
            else:
                close_handle(kernel, old_handle)
            rename_child(source_handle, destination_root, destination.name)
        finally:
            close_handle(kernel, source_handle)
        published = _open(
            kernel,
            advapi,
            destination_root,
            destination.name,
            sid,
            directory=False,
        )
        close_handle(kernel, published)


def delete_private_file(path: Path) -> None:
    """Delete only a verified WorkGate-owned private file by NT object handle."""
    kernel, advapi = _libs()
    sid = _sid(kernel, advapi)
    with pinned_parent(path) as root:
        handle = _open(
            kernel,
            advapi,
            root,
            path.name,
            sid,
            directory=False,
            extra_access=0x10000,
        )
        try:
            delete_child(handle)
        finally:
            close_handle(kernel, handle)


def _verify_regular_handle(kernel: Any, handle: int) -> None:
    """Require a non-reparse regular file using attributes of its open handle."""
    tag = _AttributeTag()
    get_tag = _api(
        kernel,
        "GetFileInformationByHandleEx",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    if not get_tag(
        handle, _ATTR_TAG_INFO, ctypes.byref(tag), ctypes.sizeof(tag)
    ):
        _fail()
    if tag.FileAttributes & _REPARSE:
        raise PermissionError("Refusing Windows transfer reparse point")
    if tag.FileAttributes & _DIRECTORY:
        raise ValueError("Transfer requires a regular file")


def replace_workspace_file(source: Path, destination: Path) -> None:
    """Publish a protected staging file into a workspace via verified NT handles."""
    kernel, advapi = _libs()
    sid = _sid(kernel, advapi)
    with (
        pinned_parent(source) as source_parent,
        pinned_parent(destination) as destination_parent,
    ):
        source_handle = _open(
            kernel,
            advapi,
            source_parent,
            source.name,
            sid,
            directory=False,
            extra_access=0x10000,
        )
        try:
            try:
                existing = open_child(
                    destination_parent,
                    destination.name,
                    access=0x80,
                    directory=False,
                )
            except FileNotFoundError:
                pass
            else:
                try:
                    _verify_regular_handle(kernel, existing)
                finally:
                    close_handle(kernel, existing)
            rename_child(source_handle, destination_parent, destination.name)
        finally:
            close_handle(kernel, source_handle)


def open_regular_file_no_follow(path: Path, *, writable: bool = False) -> int:
    """Open a regular workspace file through verified NT parent and child handles."""
    import msvcrt

    kernel, _advapi = _libs()
    with pinned_parent(path) as parent:
        handle = open_child(
            parent,
            path.name,
            access=_GENERIC_READ | (_GENERIC_WRITE if writable else 0),
            directory=False,
        )
        try:
            _verify_regular_handle(kernel, handle)
            mode = os.O_RDWR if writable else os.O_RDONLY
            return cast(Any, msvcrt).open_osfhandle(
                handle, mode | cast(Any, os).O_BINARY
            )
        except BaseException:
            close_handle(kernel, handle)
            raise
