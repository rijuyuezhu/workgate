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
    write_security: bool = False,
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
            # Staged ACL-copy files also need DELETE on the *same* handle,
            # even after source DACL permissions have been applied.
            extra_access=0xD0000 if write_security else 0,
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


def replace_workspace_file(
    source: Path,
    destination: Path,
    *,
    private_source: bool = True,
    replace: bool = True,
    posix_semantics: bool = False,
) -> None:
    """Publish a verified file via NT handles; honor exclusive new-file renames."""
    kernel, advapi = _libs()
    sid = _sid(kernel, advapi)
    with (
        pinned_parent(source) as source_parent,
        pinned_parent(destination) as destination_parent,
    ):
        if private_source:
            source_handle = _open(
                kernel,
                advapi,
                source_parent,
                source.name,
                sid,
                directory=False,
                extra_access=0x10000,
            )
        else:
            source_handle = open_child(
                source_parent,
                source.name,
                access=_GENERIC_READ | 0x10000,
                directory=False,
            )
            try:
                _verify_regular_handle(kernel, source_handle)
            except BaseException:
                close_handle(kernel, source_handle)
                raise
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
            rename_child(
                source_handle,
                destination_parent,
                destination.name,
                replace=replace,
                posix_semantics=posix_semantics,
            )
        finally:
            close_handle(kernel, source_handle)


def open_regular_file_no_follow(
    path: Path, *, writable: bool = False, share_write: bool = True
) -> int:
    """Open a verified regular file, optionally denying concurrent writers."""
    import msvcrt

    kernel, _advapi = _libs()
    with pinned_parent(path) as parent:
        handle = open_child(
            parent,
            path.name,
            access=_GENERIC_READ | (_GENERIC_WRITE if writable else 0),
            directory=False,
            share_access=7 if share_write else 5,
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


def create_workspace_file_no_follow(
    path: Path, *, preserve_owner: bool = False
) -> int:
    """Exclusively create one regular user file with verified native parent handles.

    Uses the destination directory's native inherited ACL; private state files
    instead go through open_private_file, which sets an explicit protected ACL.
    """
    import msvcrt

    kernel, _advapi = _libs()
    with pinned_parent(path) as parent:
        handle = open_child(
            parent,
            path.name,
            access=_GENERIC_READ
            | _GENERIC_WRITE
            | 0x10000
            | (0xC0000 if preserve_owner else 0),
            directory=False,
            create=True,
            exclusive=True,
        )
        try:
            _verify_regular_handle(kernel, handle)
            return cast(Any, msvcrt).open_osfhandle(
                handle, os.O_WRONLY | cast(Any, os).O_BINARY
            )
        except BaseException:
            close_handle(kernel, handle)
            raise


def delete_open_workspace_file(descriptor: int) -> None:
    """Delete the *same open file object* on write failure, never reopen its path."""
    import msvcrt

    kernel, _advapi = _libs()
    handle = cast(Any, msvcrt).get_osfhandle(descriptor)
    _verify_regular_handle(kernel, handle)
    delete_child(handle)


def copy_handle_owner_dacl(source: int, destination: int) -> None:
    """Copy owner and protected/unprotected DACL between verified NT handles."""
    kernel, advapi = _libs()
    get_security = _api(
        advapi,
        "GetSecurityInfo",
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
    )
    set_security = _api(
        advapi,
        "SetSecurityInfo",
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    )
    free = _api(kernel, "LocalFree", wintypes.HANDLE, wintypes.HANDLE)
    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    descriptor = wintypes.LPVOID()
    code = get_security(
        source,
        1,
        5,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if code:
        raise OSError(code, cast(Any, ctypes).FormatError(code))
    try:
        # The source may have *inherited* ACEs. Letting Windows re-inherit
        # from our owner-private staging parent would discard those entries,
        # leaving an inaccessible copy. Protect the copied effective DACL
        # until the destination can be published without changing its grants.
        code = set_security(
            destination, 1, 5 | 0x80000000, owner, None, dacl, None
        )
        if code:
            raise OSError(code, cast(Any, ctypes).FormatError(code))
    finally:
        free(ctypes.cast(descriptor, wintypes.HANDLE))


def _copy_workspace_security(source_fd: int, destination_fd: int) -> None:
    import msvcrt

    copy_handle_owner_dacl(
        cast(Any, msvcrt).get_osfhandle(source_fd),
        cast(Any, msvcrt).get_osfhandle(destination_fd),
    )


def atomic_write_workspace_bytes(
    path: Path,
    data: bytes,
    *,
    overwrite: bool,
    expected_sha256: str | None = None,
) -> bool:
    """Atomically publish user bytes via NT handles, preserving an existing DACL."""
    import hashlib
    import secrets

    previous_fd: int | None = None
    try:
        with contextlib.suppress(FileNotFoundError):
            previous_fd = open_regular_file_no_follow(path, share_write=False)
        if previous_fd is not None and not overwrite:
            raise FileExistsError(str(path))
        if expected_sha256 is not None:
            if previous_fd is None:
                raise ValueError("File changed; reload before saving")
            with os.fdopen(os.dup(previous_fd), "rb") as reader:
                if (
                    hashlib.file_digest(reader, "sha256").hexdigest()
                    != expected_sha256
                ):
                    raise ValueError("File changed; reload before saving")

        temp = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
        descriptor = create_workspace_file_no_follow(
            temp, preserve_owner=previous_fd is not None
        )
        with os.fdopen(descriptor, "wb") as output:
            try:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
                if previous_fd is not None:
                    _copy_workspace_security(previous_fd, output.fileno())
                if expected_sha256 is not None:
                    assert previous_fd is not None
                    # A cooperating or hostile writer might have swapped the file
                    # after snapshot validation. Do not publish over a different
                    # object even when it happens to have the same contents.
                    current_fd = open_regular_file_no_follow(
                        path, share_write=False
                    )
                    try:
                        if not os.path.samestat(
                            os.fstat(previous_fd), os.fstat(current_fd)
                        ):
                            raise ValueError(
                                "File changed; reload before saving"
                            )
                    finally:
                        os.close(current_fd)
                replace_workspace_file(
                    temp,
                    path,
                    private_source=False,
                    replace=overwrite,
                    posix_semantics=overwrite,
                )
            except BaseException:
                delete_open_workspace_file(output.fileno())
                raise
        return previous_fd is None
    finally:
        if previous_fd is not None:
            os.close(previous_fd)


def delete_workspace_file(path: Path) -> None:
    """Delete the exact regular-file object opened relative to verified parents."""
    kernel, _advapi = _libs()
    with pinned_parent(path) as parent:
        handle = open_child(
            parent,
            path.name,
            access=0x80 | 0x10000,
            directory=False,
        )
        try:
            _verify_regular_handle(kernel, handle)
            delete_child(handle)
        finally:
            close_handle(kernel, handle)


def copy_exact_file(source: Any, destination: Any, size: int) -> None:
    """Copy the pinned byte length, refusing a changed source size."""
    remaining = size
    while remaining:
        chunk = source.read(min(remaining, 1024 * 1024))
        if not chunk:
            raise RuntimeError("Windows file copy source was truncated")
        destination.write(chunk)
        remaining -= len(chunk)
    if source.read(1):
        raise RuntimeError("Windows file copy source grew during copy")


def copy_workspace_file(source: Path, destination: Path) -> None:
    """Copy one verified regular file, publishing atomically without clobber.

    Stage with a private DACL until all content and source ACL metadata have
    been copied. Failure marks only the *open* stage object for deletion.
    """
    import msvcrt
    import secrets

    stage = destination.with_name(
        f".{destination.name}.copy-{secrets.token_hex(12)}"
    )
    with (
        os.fdopen(
            open_regular_file_no_follow(source, share_write=False), "rb"
        ) as reader,
        os.fdopen(
            open_private_file(
                stage, create=True, exclusive=True, write_security=True
            ),
            "wb",
        ) as writer,
    ):
        try:
            before = os.fstat(reader.fileno())
            copy_exact_file(reader, writer, before.st_size)
            writer.flush()
            os.fsync(writer.fileno())
            after = os.fstat(reader.fileno())
            if (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise RuntimeError(
                    "Source file changed during native Windows copy"
                )
            _copy_workspace_security(reader.fileno(), writer.fileno())
            # Publish the same already-open stage object; do not reopen its
            # unpredictable pathname and introduce a source-substitution race.
            with pinned_parent(destination) as parent:
                rename_child(
                    cast(Any, msvcrt).get_osfhandle(writer.fileno()),
                    parent,
                    destination.name,
                    replace=False,
                )
        except BaseException:
            delete_open_workspace_file(writer.fileno())
            raise


def _open_reparse_entry(kernel: Any, parent: int, name: str) -> int:
    """Open only a link/junction object by NT handle, never follow its target."""
    rights = 0x80 | 0x10000
    try:
        handle = open_child(parent, name, access=rights, directory=True)
    except NotADirectoryError:
        handle = open_child(parent, name, access=rights, directory=False)
    try:
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
        if not get_tag(
            handle, _ATTR_TAG_INFO, ctypes.byref(tag), ctypes.sizeof(tag)
        ):
            _fail()
        if not tag.FileAttributes & _REPARSE:
            raise PermissionError(
                "Windows link changed to a normal object before mutation"
            )
        return handle
    except BaseException:
        close_handle(kernel, handle)
        raise


def delete_workspace_link(path: Path) -> None:
    """Remove only a verified Windows reparse entry; leave its target untouched."""
    kernel, _advapi = _libs()
    with pinned_parent(path) as parent:
        handle = _open_reparse_entry(kernel, parent, path.name)
        try:
            delete_child(handle)
        finally:
            close_handle(kernel, handle)


def move_workspace_link(source: Path, destination: Path) -> None:
    """Rename a verified link/junction without following or clobbering targets."""
    kernel, _advapi = _libs()
    with (
        pinned_parent(source) as source_parent,
        pinned_parent(destination) as destination_parent,
    ):
        handle = _open_reparse_entry(kernel, source_parent, source.name)
        try:
            rename_child(
                handle, destination_parent, destination.name, replace=False
            )
        finally:
            close_handle(kernel, handle)


def read_private_bytes(path: Path, *, max_bytes: int | None = None) -> bytes:
    """Bounded native Windows owner-only read from a single verified handle."""
    with os.fdopen(open_private_file(path, create=False), "rb") as reader:
        if (
            max_bytes is not None
            and os.fstat(reader.fileno()).st_size > max_bytes
        ):
            raise ValueError("Private file exceeds read size limit")
        data = reader.read(max_bytes + 1 if max_bytes is not None else -1)
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError("Private file exceeds read size limit")
    return data
