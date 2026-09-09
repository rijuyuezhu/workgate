"""Binary-safe transactional primitives for session and remote-worker transfers."""

import base64
import binascii
import contextlib
import ctypes
import hashlib
import importlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from ..protocol.transfer import (
    DEFAULT_TRANSFER_CHUNK_BYTES,
    normalize_chunk_size,
)
from ..schemas.result_models.transfer import (
    TransferAbortWriteOutput,
    TransferAllocTempPathOutput,
    TransferBeginWriteOutput,
    TransferCopyFileOutput,
    TransferDeleteTempPathOutput,
    TransferFinishWriteOutput,
    TransferPackDirOutput,
    TransferReadChunkOutput,
    TransferStatOutput,
    TransferUnpackArchiveOutput,
    TransferWriteChunkOutput,
)
from ..tool_session.store import ToolSessionStore
from ..utils.path_locks import path_lock, path_locks
from ..utils.path_policy import (
    relative_display_from_root,
    resolve_path_with_policy,
)
from ..utils.private_files import atomic_write_private_text
from .config import ExecutorConfig
from .path import prune_temp_dir, temp_dir

_TRANSFER_TMP_MARKER = "workgate-transfer"
_TRANSFER_STALE_GRACE_S = 24 * 60 * 60
_TRANSFER_TMP_PRUNE_MINIMUM_AGE_S = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class TransferContext:
    """Explicit executor authority used by final transfer composition."""

    config: ExecutorConfig
    store: ToolSessionStore


def _policy_path(
    context: TransferContext,
    path: str | Path,
    *,
    must_exist: bool = False,
    follow_final_symlink: bool = True,
) -> Path:
    return resolve_path_with_policy(
        path,
        workspace_root=context.config.workspace_root,
        allow_full_control=context.config.allow_full_control,
        path_denylist=context.config.path_denylist,
        must_exist=must_exist,
        follow_final_symlink=follow_final_symlink,
    )


def _display(path: Path, context: TransferContext) -> str:
    return relative_display_from_root(path, context.config.workspace_root)


def _scratch_dir(context: TransferContext) -> Path:
    return temp_dir(context.config.temp_dir)


def _prune_transfer_scratch(context: TransferContext) -> None:
    prune_temp_dir(
        minimum_age_s=_TRANSFER_TMP_PRUNE_MINIMUM_AGE_S,
        max_files=context.config.max_tmp_files,
        max_bytes=context.config.max_tmp_bytes,
        directory=context.config.temp_dir,
    )


def _sha256_file(
    path: Path, chunk_size: int = DEFAULT_TRANSFER_CHUNK_BYTES
) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_temp_path(
    path: str | Path,
    *,
    must_exist: bool = False,
    context: TransferContext,
) -> Path:
    """Resolve a transfer scratch path under the configured temp directory."""
    raw = Path(os.path.expandvars(os.path.expanduser(str(path))))
    if not raw.is_absolute():
        raw = context.config.workspace_root / raw
    resolved = raw.resolve(strict=False)
    base = _scratch_dir(context).resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Path is not a transfer temp path: {path}") from exc
    if must_exist and not resolved.exists():
        raise FileNotFoundError(str(resolved))
    return resolved


def _resolve_transfer_path(
    path: str | Path,
    *,
    must_exist: bool = False,
    session_id: str | None = None,
    workdir: str | None = None,
    allow_temp: bool = True,
    context: TransferContext,
) -> Path:
    """Resolve one readable user/session path, allowing internal scratch paths."""
    if session_id is not None and workdir is not None:
        raise ValueError("session_id and workdir are mutually exclusive")
    if session_id is not None:
        store = context.store
        session = store.touch_session(session_id)
        return store.resolve_session_path(session, path, must_exist=must_exist)
    if workdir is not None:
        base = _policy_path(context, workdir, must_exist=True)
        raw = Path(path)
        candidate = raw if raw.is_absolute() else base / raw
        resolved = _policy_path(context, candidate, must_exist=must_exist)
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise ValueError(f"Path escapes session workdir: {path}") from exc
        return resolved
    try:
        return _policy_path(context, path, must_exist=must_exist)
    except ValueError:
        if allow_temp:
            return _resolve_temp_path(
                path, must_exist=must_exist, context=context
            )
        raise


def _resolve_transfer_destination(
    path: str | Path,
    *,
    session_id: str | None = None,
    workdir: str | None = None,
    allow_temp: bool = True,
    context: TransferContext,
) -> Path:
    """Resolve a destination without following its final directory entry."""
    if session_id is not None and workdir is not None:
        raise ValueError("session_id and workdir are mutually exclusive")
    if session_id is not None:
        store = context.store
        session = store.touch_session(session_id)
        return store.resolve_session_path(
            session, path, follow_final_symlink=False
        )
    if workdir is not None:
        base = _policy_path(context, workdir, must_exist=True)
        raw = Path(path)
        candidate = raw if raw.is_absolute() else base / raw
        resolved = _policy_path(context, candidate, follow_final_symlink=False)
        try:
            resolved.parent.relative_to(base)
        except ValueError as exc:
            raise ValueError(f"Path escapes session workdir: {path}") from exc
        return resolved
    try:
        return _policy_path(context, path, follow_final_symlink=False)
    except ValueError:
        if allow_temp:
            return _resolve_temp_path(path, context=context)
        raise


def transfer_stat(
    path: str,
    sha256: bool = True,
    *,
    session_id: str | None = None,
    workdir: str | None = None,
    context: TransferContext,
) -> TransferStatOutput:
    """Return transfer metadata for one workspace or session path."""
    source = _resolve_transfer_path(
        path,
        must_exist=True,
        session_id=session_id,
        workdir=workdir,
        context=context,
    )
    source_stat = source.stat()
    if source.is_file():
        return TransferStatOutput(
            path=_display(source, context),
            type="file",
            size=source_stat.st_size,
            modified=source_stat.st_mtime,
            sha256=_sha256_file(source) if sha256 else None,
        )
    if source.is_dir():
        return TransferStatOutput(
            path=_display(source, context),
            type="dir",
            size=None,
            modified=source_stat.st_mtime,
        )
    return TransferStatOutput(
        path=_display(source, context),
        type="other",
        size=source_stat.st_size,
        modified=source_stat.st_mtime,
    )


def transfer_read_chunk(
    path: str,
    offset: int = 0,
    chunk_size: int | None = None,
    *,
    session_id: str | None = None,
    workdir: str | None = None,
    context: TransferContext,
) -> TransferReadChunkOutput:
    """Read one bounded binary chunk and encode it for transport."""
    source = _resolve_transfer_path(
        path,
        must_exist=True,
        session_id=session_id,
        workdir=workdir,
        context=context,
    )
    if not source.is_file():
        raise IsADirectoryError(str(source))
    size = source.stat().st_size
    start = int(offset)
    if start < 0:
        raise ValueError("offset must be >= 0")
    if start > size:
        raise ValueError(f"offset {start} exceeds source size {size}")
    limit = normalize_chunk_size(chunk_size)
    with source.open("rb") as handle:
        handle.seek(start)
        data = handle.read(limit)
    digest = hashlib.sha256(data).hexdigest()
    return TransferReadChunkOutput(
        path=_display(source, context),
        offset=start,
        bytes=len(data),
        size=size,
        eof=start + len(data) >= size,
        sha256=digest,
        data_b64=base64.b64encode(data).decode("ascii"),
    )


def _transfer_temp_path(destination: Path, transfer_id: str) -> Path:
    safe_id = "".join(
        character
        for character in transfer_id
        if character.isalnum() or character in "-_"
    )
    if not safe_id or safe_id != transfer_id:
        raise ValueError("transfer_id contains unsupported characters")
    return (
        destination.parent
        / f".{destination.name}.{_TRANSFER_TMP_MARKER}-{safe_id}.tmp"
    )


def _transfer_metadata_path(temporary: Path) -> Path:
    return temporary.with_name(temporary.name + ".json")


def _write_transfer_metadata(temporary: Path, metadata: dict[str, Any]) -> None:
    atomic_write_private_text(
        _transfer_metadata_path(temporary),
        json.dumps(metadata, sort_keys=True, separators=(",", ":")),
    )


def _read_transfer_metadata(
    temporary: Path, destination: Path
) -> dict[str, Any]:
    metadata_path = _transfer_metadata_path(temporary)
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"transfer metadata is missing or invalid: {metadata_path}"
        ) from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"transfer metadata is invalid: {metadata_path}")
    if metadata.get("destination") != str(destination):
        raise ValueError("transfer metadata destination mismatch")
    return metadata


def _received_ranges(metadata: dict[str, Any]) -> list[list[int]]:
    raw = metadata.get("received_ranges", [])
    if not isinstance(raw, list):
        raise ValueError("transfer received_ranges metadata is invalid")
    ranges: list[list[int]] = []
    previous_end = -1
    for item in raw:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(value, int) for value in item)
        ):
            raise ValueError("transfer received_ranges metadata is invalid")
        start, end = item
        if start < 0 or end < start or start < previous_end:
            raise ValueError("transfer received_ranges metadata is invalid")
        ranges.append([start, end])
        previous_end = end
    return ranges


def _record_received_range(
    metadata: dict[str, Any], start: int, end: int
) -> None:
    if end < start:
        raise ValueError("transfer range is invalid")
    if end == start:
        return
    ranges = _received_ranges(metadata)
    for existing_start, existing_end in ranges:
        if start < existing_end and end > existing_start:
            raise ValueError("transfer chunk overlaps previously received data")
    ranges.append([start, end])
    ranges.sort()
    merged: list[list[int]] = []
    for range_start, range_end in ranges:
        if merged and merged[-1][1] == range_start:
            merged[-1][1] = range_end
        else:
            merged.append([range_start, range_end])
    metadata["received_ranges"] = merged


def _open_private_transfer_file(path: Path) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= int(getattr(os, "O_BINARY", 0))
    descriptor = os.open(path, flags, 0o600)
    return os.fdopen(descriptor, "wb")


def _open_transfer_source(path: Path) -> BinaryIO:
    if os.name == "nt":
        return _open_windows_transfer_file(path, update=False)
    flags = os.O_RDONLY
    flags |= int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags)
    try:
        source_stat = os.fstat(descriptor)
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError("transfer source path is not a regular file")
        return os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise


class _ByHandleFileInformation(ctypes.Structure):
    """Windows file identity fields returned for one open handle."""

    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


def _windows_file_information(native_handle: int) -> _ByHandleFileInformation:
    """Read stable identity and attribute fields from one Windows handle."""
    windows_ctypes: Any = ctypes
    kernel32 = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    get_information.restype = wintypes.BOOL
    information = _ByHandleFileInformation()
    if not get_information(
        wintypes.HANDLE(native_handle), ctypes.byref(information)
    ):
        error_code = int(windows_ctypes.get_last_error())
        raise OSError(error_code, "GetFileInformationByHandle failed")
    return information


def _windows_file_identity(descriptor: int) -> tuple[int, int]:
    """Return the stable Windows volume and file index for an open descriptor."""
    msvcrt = importlib.import_module("msvcrt")
    native_handle = int(msvcrt.get_osfhandle(descriptor))
    if native_handle == -1:
        raise OSError("invalid Windows file handle")
    information = _windows_file_information(native_handle)
    file_index = (int(information.nFileIndexHigh) << 32) | int(
        information.nFileIndexLow
    )
    return int(information.dwVolumeSerialNumber), file_index


def _open_windows_transfer_file(path: Path, *, update: bool) -> BinaryIO:
    """Open one Windows path entry without following a reparse point."""
    windows_ctypes: Any = ctypes
    kernel32 = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    desired_access = 0x80000000 | (0x40000000 if update else 0)
    share_mode = 0x00000001 | 0x00000002 | 0x00000004
    flags_and_attributes = 0x00200000 | 0x02000000
    native_handle = create_file(
        str(path),
        desired_access,
        share_mode,
        None,
        3,
        flags_and_attributes,
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if native_handle == invalid_handle:
        error_code = int(windows_ctypes.get_last_error())
        raise OSError(error_code, "CreateFileW failed", str(path))
    native_value = int(native_handle)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    try:
        information = _windows_file_information(native_value)
        if int(information.dwFileAttributes) & 0x00000400:
            raise ValueError(
                "transfer path must not be a symlink or reparse point"
            )
        msvcrt = importlib.import_module("msvcrt")
        descriptor = int(
            msvcrt.open_osfhandle(
                native_value,
                int(getattr(os, "O_BINARY", 0))
                | (os.O_RDWR if update else os.O_RDONLY),
            )
        )
    except Exception:
        close_handle(wintypes.HANDLE(native_value))
        raise
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("transfer path is not a regular file")
        return os.fdopen(descriptor, "r+b" if update else "rb")
    except Exception:
        os.close(descriptor)
        raise


def _transfer_handle_identity(
    handle: BinaryIO, *, platform: str | None = None
) -> tuple[int, int]:
    """Return a stable platform-native identity for an open transfer file."""
    descriptor = handle.fileno()
    effective_platform = os.name if platform is None else platform
    if effective_platform == "nt":
        return _windows_file_identity(descriptor)
    file_stat = os.fstat(descriptor)
    return int(file_stat.st_dev), int(file_stat.st_ino)


def _stable_source_identity(source_stat: os.stat_result) -> tuple[int, ...]:
    return (
        int(source_stat.st_dev),
        int(source_stat.st_ino),
        int(source_stat.st_size),
        int(source_stat.st_ctime_ns),
        int(source_stat.st_mtime_ns),
    )


def transfer_copy_file(
    source_path: str,
    destination_path: str,
    overwrite: bool = True,
    chunk_size: int | None = None,
    *,
    source_session_id: str | None = None,
    destination_session_id: str | None = None,
    source_workdir: str | None = None,
    destination_workdir: str | None = None,
    context: TransferContext,
) -> TransferCopyFileOutput:
    """Stream a file within one worker and atomically publish the destination."""
    source = _resolve_transfer_path(
        source_path,
        must_exist=True,
        session_id=source_session_id,
        workdir=source_workdir,
        context=context,
    )
    destination = _resolve_transfer_destination(
        destination_path,
        session_id=destination_session_id,
        workdir=destination_workdir,
        context=context,
    )
    limit = normalize_chunk_size(chunk_size)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with path_locks([source, destination]):
        if source == destination:
            if not overwrite:
                raise FileExistsError(str(destination))
            source_stat = source.stat()
            if not stat.S_ISREG(source_stat.st_mode):
                raise ValueError("transfer source path is not a regular file")
            size = int(source_stat.st_size)
            return TransferCopyFileOutput(
                source_path=_display(source, context),
                path=_display(destination, context),
                bytes=size,
                sha256=_sha256_file(source, limit),
                chunks=0 if size == 0 else (size + limit - 1) // limit,
                chunk_size=limit,
                completed=True,
            )

        exists = os.path.lexists(destination)
        if exists and destination.is_dir() and not destination.is_symlink():
            raise IsADirectoryError(str(destination))
        if exists and not overwrite:
            raise FileExistsError(str(destination))

        temporary = _transfer_temp_path(destination, uuid.uuid4().hex)
        digest = hashlib.sha256()
        copied = 0
        chunks = 0
        try:
            with _open_transfer_source(source) as source_handle:
                initial_source_stat = os.fstat(source_handle.fileno())
                with _open_private_transfer_file(
                    temporary
                ) as destination_handle:
                    while chunk := source_handle.read(limit):
                        destination_handle.write(chunk)
                        digest.update(chunk)
                        copied += len(chunk)
                        chunks += 1
                    destination_handle.flush()
                    os.fsync(destination_handle.fileno())
                final_source_stat = os.fstat(source_handle.fileno())

            if _stable_source_identity(
                initial_source_stat
            ) != _stable_source_identity(final_source_stat):
                raise ValueError("transfer source file changed during copy")
            if copied != int(initial_source_stat.st_size):
                raise ValueError(
                    f"size mismatch: expected {initial_source_stat.st_size}, got {copied}"
                )
            if not overwrite and os.path.lexists(destination):
                raise FileExistsError(str(destination))
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    return TransferCopyFileOutput(
        source_path=_display(source, context),
        path=_display(destination, context),
        bytes=copied,
        sha256=digest.hexdigest(),
        chunks=chunks,
        chunk_size=limit,
        completed=True,
    )


def _validate_transfer_identity(
    handle: BinaryIO, metadata: dict[str, Any]
) -> os.stat_result:
    """Verify an open temporary file still matches its stable identity and size."""
    file_stat = os.fstat(handle.fileno())
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError("transfer temporary path is not a regular file")
    expected_identity = (
        int(metadata.get("temporary_identity_a", -1)),
        int(metadata.get("temporary_identity_b", -1)),
    )
    if expected_identity != _transfer_handle_identity(handle):
        raise ValueError("transfer temporary file identity changed")
    if int(metadata.get("temporary_size", -1)) != int(file_stat.st_size):
        raise ValueError("transfer temporary file size changed")
    return file_stat


def _sha256_handle(handle: BinaryIO) -> str:
    """Hash an open transfer file without reopening its path."""
    digest = hashlib.sha256()
    handle.seek(0)
    while chunk := handle.read(DEFAULT_TRANSFER_CHUNK_BYTES):
        digest.update(chunk)
    handle.seek(0)
    return digest.hexdigest()


def _prune_stale_destination_transfers(destination: Path) -> None:
    prune_before = time.time() - _TRANSFER_STALE_GRACE_S
    pattern = f".{destination.name}.{_TRANSFER_TMP_MARKER}-*.tmp"
    for temporary in destination.parent.glob(pattern):
        try:
            if temporary.stat().st_mtime > prune_before:
                continue
            temporary.unlink(missing_ok=True)
            _transfer_metadata_path(temporary).unlink(missing_ok=True)
        except OSError:
            continue
    metadata_pattern = pattern + ".json"
    for metadata_path in destination.parent.glob(metadata_pattern):
        temporary = Path(str(metadata_path).removesuffix(".json"))
        try:
            if (
                temporary.exists()
                or metadata_path.stat().st_mtime > prune_before
            ):
                continue
            metadata_path.unlink(missing_ok=True)
        except OSError:
            continue


def transfer_begin_write(
    path: str,
    overwrite: bool = True,
    expected_bytes: int | None = None,
    transfer_id: str | None = None,
    *,
    session_id: str | None = None,
    workdir: str | None = None,
    context: TransferContext,
) -> TransferBeginWriteOutput:
    """Create or safely resume a private transactional chunked write."""
    destination = _resolve_transfer_destination(
        path,
        session_id=session_id,
        workdir=workdir,
        context=context,
    )
    expected = None if expected_bytes is None else int(expected_bytes)
    if expected is not None and expected < 0:
        raise ValueError("expected_bytes must be >= 0")
    destination.parent.mkdir(parents=True, exist_ok=True)
    requested_id = transfer_id or uuid.uuid4().hex
    temporary = _transfer_temp_path(destination, requested_id)
    with path_locks([destination, temporary]):
        _prune_stale_destination_transfers(destination)
        exists = os.path.lexists(destination)
        if exists and destination.is_dir() and not destination.is_symlink():
            raise IsADirectoryError(str(destination))
        if exists and not overwrite:
            raise FileExistsError(str(destination))
        if transfer_id is not None and temporary.exists():
            metadata = _read_transfer_metadata(temporary, destination)
            if bool(metadata.get("overwrite", True)) != bool(overwrite):
                raise ValueError("transfer overwrite mode mismatch")
            metadata_expected = metadata.get("expected_bytes")
            if metadata_expected != expected:
                raise ValueError("transfer expected size mismatch")
            with _open_transfer_for_update(temporary) as handle:
                _validate_transfer_identity(handle, metadata)
            ranges = _received_ranges(metadata)
            offset = 0 if ranges == [] else ranges[0][1]
            if ranges not in ([], [[0, offset]]):
                raise ValueError("transfer resume ranges are not contiguous")
            return TransferBeginWriteOutput(
                path=_display(destination, context),
                temp_path=_display(temporary, context),
                transfer_id=requested_id,
                created=not exists,
                expected_bytes=expected,
                offset=offset,
                resumed=True,
            )
        with _open_private_transfer_file(temporary) as handle:
            temporary_stat = os.fstat(handle.fileno())
            temporary_identity = _transfer_handle_identity(handle)
        try:
            _write_transfer_metadata(
                temporary,
                {
                    "destination": str(destination),
                    "overwrite": bool(overwrite),
                    "expected_bytes": expected,
                    "received_ranges": [],
                    "created_at": time.time(),
                    "temporary_identity_a": temporary_identity[0],
                    "temporary_identity_b": temporary_identity[1],
                    "temporary_size": int(temporary_stat.st_size),
                },
            )
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    return TransferBeginWriteOutput(
        path=_display(destination, context),
        temp_path=_display(temporary, context),
        transfer_id=requested_id,
        created=not exists,
        expected_bytes=expected,
        offset=0,
        resumed=False,
    )


def _open_transfer_for_update(path: Path) -> BinaryIO:
    if os.name == "nt":
        return _open_windows_transfer_file(path, update=True)
    if path.is_symlink():
        raise ValueError("transfer temporary path must not be a symlink")
    flags = os.O_RDWR
    flags |= int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("transfer temporary path is not a regular file")
        return os.fdopen(descriptor, "r+b")
    except Exception:
        os.close(descriptor)
        raise


def transfer_write_bytes(
    path: str,
    transfer_id: str,
    offset: int,
    data: bytes,
    expected_sha256: str | None = None,
    *,
    session_id: str | None = None,
    workdir: str | None = None,
    context: TransferContext,
) -> TransferWriteChunkOutput:
    """Write one raw validated chunk into an active transactional destination."""
    destination = _resolve_transfer_destination(
        path,
        session_id=session_id,
        workdir=workdir,
        context=context,
    )
    temporary = _transfer_temp_path(destination, transfer_id)
    start = int(offset)
    if start < 0:
        raise ValueError("offset must be >= 0")
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 and digest != expected_sha256:
        raise ValueError("chunk sha256 mismatch")

    with path_lock(temporary):
        if not temporary.exists():
            raise FileNotFoundError(str(temporary))
        metadata = _read_transfer_metadata(temporary, destination)
        expected = metadata.get("expected_bytes")
        if expected is not None and start + len(data) > int(expected):
            raise ValueError("chunk exceeds expected transfer size")
        _record_received_range(metadata, start, start + len(data))
        with _open_transfer_for_update(temporary) as handle:
            _validate_transfer_identity(handle, metadata)
            handle.seek(start)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            updated_stat = os.fstat(handle.fileno())
            metadata["temporary_size"] = int(updated_stat.st_size)
        _write_transfer_metadata(temporary, metadata)
    return TransferWriteChunkOutput(
        path=_display(destination, context),
        temp_path=_display(temporary, context),
        offset=start,
        bytes=len(data),
        sha256=digest,
    )


def transfer_write_chunk(
    path: str,
    transfer_id: str,
    offset: int,
    data_b64: str,
    expected_sha256: str | None = None,
    *,
    session_id: str | None = None,
    workdir: str | None = None,
    context: TransferContext,
) -> TransferWriteChunkOutput:
    """Decode and write one validated chunk from the JSON control channel."""
    try:
        data = base64.b64decode(data_b64.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError("data_b64 is not valid base64") from exc
    return transfer_write_bytes(
        path,
        transfer_id,
        offset,
        data,
        expected_sha256,
        session_id=session_id,
        workdir=workdir,
        context=context,
    )


def transfer_finish_write(
    path: str,
    transfer_id: str,
    expected_bytes: int | None = None,
    expected_sha256: str | None = None,
    *,
    session_id: str | None = None,
    workdir: str | None = None,
    context: TransferContext,
) -> TransferFinishWriteOutput:
    """Validate and atomically publish a completed chunked write."""
    destination = _resolve_transfer_destination(
        path,
        session_id=session_id,
        workdir=workdir,
        context=context,
    )
    temporary = _transfer_temp_path(destination, transfer_id)
    metadata_path = _transfer_metadata_path(temporary)
    with path_locks([destination, temporary]):
        if not temporary.exists():
            raise FileNotFoundError(str(temporary))
        metadata = _read_transfer_metadata(temporary, destination)
        metadata_expected = metadata.get("expected_bytes")
        requested_expected = (
            None if expected_bytes is None else int(expected_bytes)
        )
        if (
            metadata_expected is not None
            and requested_expected is not None
            and int(metadata_expected) != requested_expected
        ):
            raise ValueError("expected_bytes does not match transfer metadata")
        expected = (
            requested_expected
            if requested_expected is not None
            else metadata_expected
        )
        if expected is not None:
            expected = int(expected)
        with _open_transfer_for_update(temporary) as handle:
            temporary_stat = _validate_transfer_identity(handle, metadata)
            size = int(temporary_stat.st_size)
            if expected is not None and size != expected:
                raise ValueError(
                    f"size mismatch: expected {expected}, got {size}"
                )
            required_size = size if expected is None else expected
            received = _received_ranges(metadata)
            complete = (
                received == []
                if required_size == 0
                else received == [[0, required_size]]
            )
            if not complete:
                raise ValueError(
                    "transfer has missing or non-contiguous data ranges"
                )
            digest = _sha256_handle(handle) if expected_sha256 else None
            if expected_sha256 and digest != expected_sha256:
                raise ValueError("file sha256 mismatch")
        with _open_transfer_for_update(temporary) as handle:
            _validate_transfer_identity(handle, metadata)
        if not bool(metadata.get("overwrite", True)) and os.path.lexists(
            destination
        ):
            raise FileExistsError(str(destination))
        os.replace(temporary, destination)
        metadata_path.unlink(missing_ok=True)
    return TransferFinishWriteOutput(
        path=_display(destination, context),
        bytes=size,
        sha256=digest,
        completed=True,
    )


def transfer_abort_write(
    path: str,
    transfer_id: str,
    *,
    session_id: str | None = None,
    workdir: str | None = None,
    context: TransferContext,
) -> TransferAbortWriteOutput:
    """Abort an active chunked write and remove its private state."""
    destination = _resolve_transfer_destination(
        path,
        session_id=session_id,
        workdir=workdir,
        context=context,
    )
    temporary = _transfer_temp_path(destination, transfer_id)
    metadata_path = _transfer_metadata_path(temporary)
    deleted = False
    with path_lock(temporary):
        if temporary.exists():
            temporary.unlink()
            deleted = True
        metadata_path.unlink(missing_ok=True)
    return TransferAbortWriteOutput(
        path=_display(destination, context),
        temp_path=_display(temporary, context),
        deleted=deleted,
    )


def _assert_transferable_tree(source: Path, context: TransferContext) -> None:
    """Reject symlinks and special files before creating an archive."""
    if source.is_symlink():
        raise ValueError(
            f"refusing to pack symlink: {_display(source, context)}"
        )
    for child in source.rglob("*"):
        if child.is_symlink():
            raise ValueError(
                f"refusing to pack directory containing symlink: {_display(child, context)}"
            )
        mode = child.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError(
                f"refusing to pack special file: {_display(child, context)}"
            )


def transfer_alloc_temp_path(
    suffix: str = ".bin",
    *,
    session_id: str | None = None,
    context: TransferContext,
) -> TransferAllocTempPathOutput:
    """Allocate a safe temporary workspace path for transfer scratch data."""
    _ = session_id
    _prune_transfer_scratch(context)
    safe_suffix = (
        suffix
        if suffix.startswith(".") and "/" not in suffix and "\\" not in suffix
        else ".bin"
    )
    path = (
        _scratch_dir(context)
        / f"remote-transfer-{uuid.uuid4().hex}{safe_suffix}"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return TransferAllocTempPathOutput(path=_display(path, context))


def transfer_delete_temp_path(
    path: str, *, context: TransferContext
) -> TransferDeleteTempPathOutput:
    """Delete a transfer scratch file under the configured temp directory."""
    scratch = _resolve_temp_path(path, must_exist=False, context=context)
    deleted = False
    if scratch.exists():
        if scratch.is_dir():
            raise IsADirectoryError(str(scratch))
        scratch.unlink()
        deleted = True
    return TransferDeleteTempPathOutput(
        path=_display(scratch, context), deleted=deleted
    )


def transfer_pack_dir(
    path: str,
    compression: str = "gz",
    *,
    session_id: str | None = None,
    workdir: str | None = None,
    context: TransferContext,
) -> TransferPackDirOutput:
    """Pack a directory into a bounded temporary archive for transfer."""
    _prune_transfer_scratch(context)
    if compression not in {"gz", "none"}:
        raise ValueError("compression must be 'gz' or 'none'")
    source = _resolve_transfer_path(
        path,
        must_exist=True,
        session_id=session_id,
        workdir=workdir,
        context=context,
    )
    if not source.is_dir():
        raise NotADirectoryError(str(source))
    _assert_transferable_tree(source, context)
    suffix = ".tar.gz" if compression == "gz" else ".tar"
    mode = "w:gz" if compression == "gz" else "w"
    archive = (
        _scratch_dir(context) / f"transfer-pack-{uuid.uuid4().hex}{suffix}"
    )
    archive.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, mode) as tar:
            for child in source.iterdir():
                tar.add(child, arcname=child.name, recursive=True)
        size = archive.stat().st_size
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    return TransferPackDirOutput(
        path=_display(source, context),
        archive_path=_display(archive, context),
        bytes=size,
        sha256=_sha256_file(archive),
        compression=compression,
    )


def _safe_members(
    tar: tarfile.TarFile,
    staging: Path,
    context: TransferContext,
) -> list[tarfile.TarInfo]:
    settings = context.config
    base = staging.resolve(strict=False)
    maximum_entries = max(1, settings.max_transfer_archive_entries)
    total_bytes = 0
    seen_paths: set[str] = set()
    safe: list[tarfile.TarInfo] = []
    for member in tar:
        if len(safe) >= maximum_entries:
            raise ValueError(
                f"archive contains more than {maximum_entries} entries"
            )
        member_path = Path(member.name)
        if member_path.is_absolute() or ".." in member_path.parts:
            raise ValueError(f"unsafe archive member path: {member.name}")
        if member.issym() or member.islnk() or member.isdev():
            raise ValueError(f"unsupported archive member type: {member.name}")
        if getattr(member, "sparse", None):
            raise ValueError(
                f"sparse archive members are not supported: {member.name}"
            )
        normalized_name = str(member_path)
        if normalized_name in seen_paths:
            raise ValueError(f"duplicate archive member path: {member.name}")
        seen_paths.add(normalized_name)
        if member.isfile():
            total_bytes += max(0, int(member.size))
            maximum_bytes = max(1, settings.max_transfer_unpacked_bytes)
            if total_bytes > maximum_bytes:
                raise ValueError(
                    f"archive expands to more than {maximum_bytes} bytes"
                )
        target = (staging / member.name).resolve(strict=False)
        try:
            target.relative_to(base)
        except ValueError as exc:
            raise ValueError(
                f"archive member escapes destination: {member.name}"
            ) from exc
        safe.append(member)
    return safe


def _remove_existing_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def transfer_unpack_archive(
    archive_path: str,
    dst_path: str,
    overwrite: bool = True,
    cleanup_archive: bool = True,
    *,
    session_id: str | None = None,
    workdir: str | None = None,
    context: TransferContext,
) -> TransferUnpackArchiveOutput:
    """Validate in staging and transactionally replace a directory destination."""
    archive = _resolve_transfer_path(
        archive_path, must_exist=True, context=context
    )
    if not archive.is_file():
        raise FileNotFoundError(str(archive))
    destination = _resolve_transfer_destination(
        dst_path,
        session_id=session_id,
        workdir=workdir,
        context=context,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.unpack-",
            dir=str(destination.parent),
        )
    )
    backup: Path | None = None
    members: list[tarfile.TarInfo] = []
    committed = False
    try:
        with tarfile.open(archive, "r:*") as tar:
            members = _safe_members(tar, staging, context)
            for member in members:
                target = staging / member.name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = tar.extractfile(member)
                    if source is None:
                        raise ValueError(
                            f"archive member has no file data: {member.name}"
                        )
                    with source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    os.chmod(target, member.mode & 0o777)
                    continue
                raise ValueError(
                    f"unsupported archive member type: {member.name}"
                )

        with path_lock(destination):
            exists = os.path.lexists(destination)
            if (
                exists
                and not overwrite
                and not (
                    destination.is_dir()
                    and not destination.is_symlink()
                    and not any(destination.iterdir())
                )
            ):
                raise FileExistsError(
                    f"destination already exists: {destination}"
                )
            if exists:
                backup = (
                    destination.parent
                    / f".{destination.name}.backup-{uuid.uuid4().hex}"
                )
                os.replace(destination, backup)
            try:
                os.replace(staging, destination)
                committed = True
            except BaseException:
                if backup is not None and os.path.lexists(backup):
                    os.replace(backup, destination)
                    backup = None
                raise

        cleanup_errors: list[str] = []
        backup_deleted = backup is None or not os.path.lexists(backup)
        if not backup_deleted and backup is not None:
            try:
                _remove_existing_path(backup)
            except OSError as exc:
                cleanup_errors.append(
                    f"could not remove replaced destination backup {backup}: {exc}"
                )
            else:
                backup = None
                backup_deleted = True

        archive_deleted = False
        if cleanup_archive:
            try:
                archive.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_errors.append(
                    f"could not remove transfer archive {archive}: {exc}"
                )
            else:
                archive_deleted = not archive.exists()
        return TransferUnpackArchiveOutput(
            path=_display(destination, context),
            archive_path=_display(archive, context),
            entries=len(members),
            completed=True,
            archive_deleted=archive_deleted,
            backup_deleted=backup_deleted,
            cleanup_errors=cleanup_errors,
        )
    finally:
        if not committed and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if not committed and backup is not None and os.path.lexists(backup):
            if not os.path.lexists(destination):
                with contextlib.suppress(OSError):
                    os.replace(backup, destination)
            if os.path.lexists(backup):
                with contextlib.suppress(OSError):
                    _remove_existing_path(backup)
