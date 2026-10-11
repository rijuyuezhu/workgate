"""POSIX helpers for private state files and lock files."""

import contextlib
import errno
import os
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO


def _retry_private_lock_io(
    deadline: float | None,
    *,
    timeout_message: str,
    retry_interval_s: float = 0.01,
) -> None:
    """Sleep before retrying one transient private-lock file operation."""
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(timeout_message) from None
        time.sleep(min(max(0.0, retry_interval_s), remaining))
    else:
        time.sleep(max(0.0, retry_interval_s))


def _ensure_lock_file(path: Path, timeout_s: float | None = None) -> None:
    """Create or repair the one-byte lock file before any process opens it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = (
        None if timeout_s is None else time.monotonic() + max(0.0, timeout_s)
    )
    timeout_message = "timed out preparing private lock file"
    while True:
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            try:
                if path.stat().st_size > 0:
                    return
                descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
            except FileNotFoundError, PermissionError:
                _retry_private_lock_io(
                    deadline, timeout_message=timeout_message
                )
                continue
        except PermissionError:
            _retry_private_lock_io(deadline, timeout_message=timeout_message)
            continue
        try:
            os.write(descriptor, b"\0")
        except PermissionError:
            _retry_private_lock_io(deadline, timeout_message=timeout_message)
            continue
        finally:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        return


def _open_lock_handle(
    path: Path,
    *,
    timeout_s: float | None = None,
    retry_interval_s: float = 0.01,
) -> BinaryIO:
    """Open an initialized lock file, retrying transient missing paths."""
    deadline = (
        None if timeout_s is None else time.monotonic() + max(0.0, timeout_s)
    )
    while True:
        try:
            return path.open("r+b")
        except FileNotFoundError, PermissionError:
            _retry_private_lock_io(
                deadline,
                timeout_message="timed out opening private lock file",
                retry_interval_s=retry_interval_s,
            )


def _is_lock_contention_error(exc: OSError) -> bool:
    """Return whether one non-blocking lock error means ordinary contention."""
    contention_errors = {
        errno.EACCES,
        errno.EAGAIN,
        getattr(errno, "EDEADLK", errno.EACCES),
        getattr(errno, "EDEADLOCK", errno.EACCES),
    }
    return isinstance(exc, BlockingIOError) or exc.errno in contention_errors


def _try_lock_handle(handle: BinaryIO) -> bool:
    """Attempt one non-blocking cross-platform file-lock acquisition."""
    handle.seek(0)
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if _is_lock_contention_error(exc):
            return False
        raise
    return True


def _lock_handle(
    handle: BinaryIO,
    *,
    timeout_s: float | None = None,
    retry_interval_s: float = 0.01,
) -> None:
    """Acquire one file lock, optionally bounding contention wait time."""
    deadline = (
        None if timeout_s is None else time.monotonic() + max(0.0, timeout_s)
    )
    while not _try_lock_handle(handle):
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out acquiring private file lock")
            time.sleep(min(max(0.0, retry_interval_s), remaining))
        else:
            time.sleep(max(0.0, retry_interval_s))


def _unlock_handle(handle: BinaryIO) -> None:
    handle.seek(0)
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def private_file_lock(
    path: Path,
    *,
    timeout_s: float | None = None,
    retry_interval_s: float = 0.01,
) -> Generator[BinaryIO]:
    """Hold one private cross-process file lock for the context lifetime."""
    started = time.monotonic()
    _ensure_lock_file(path, timeout_s=timeout_s)
    remaining = (
        None
        if timeout_s is None
        else max(0.0, timeout_s - (time.monotonic() - started))
    )
    with _open_lock_handle(
        path,
        timeout_s=remaining,
        retry_interval_s=retry_interval_s,
    ) as handle:
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        remaining = (
            None
            if timeout_s is None
            else max(0.0, timeout_s - (time.monotonic() - started))
        )
        _lock_handle(
            handle,
            timeout_s=remaining,
            retry_interval_s=retry_interval_s,
        )
        try:
            yield handle
        finally:
            _unlock_handle(handle)


def write_private_bytes(
    path: Path, data: bytes, *, exclusive: bool = False
) -> None:
    """Write or replace one private byte file using mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def append_private_bytes(path: Path, data: bytes) -> None:
    """Append bytes to one private file without changing existing content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "ab") as handle:
        handle.write(data)
        handle.flush()
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def write_private_text(path: Path, content: str) -> None:
    """Write or replace one private UTF-8 text file."""
    write_private_bytes(path, content.encode("utf-8"))


def atomic_write_private_bytes(path: Path, data: bytes) -> None:
    """Atomically replace one private file with new bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        write_private_bytes(temporary, data, exclusive=True)
        os.replace(temporary, path)
        with contextlib.suppress(OSError):
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_private_text(path: Path, content: str) -> None:
    """Atomically replace one private file with UTF-8 text."""
    atomic_write_private_bytes(path, content.encode("utf-8"))
