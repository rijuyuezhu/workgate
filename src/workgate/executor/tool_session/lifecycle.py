"""Cross-process serialization for session admission and termination."""

import asyncio
import contextlib
import hashlib
import threading
from collections.abc import AsyncGenerator, Generator, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from ...persistence import get_state_store
from ...utils.private_files import private_file_lock

_FILE_LOCK_POLL_S = 0.1
_THREAD_JOIN_POLL_S = 0.01


@dataclass
class _LifecycleEntry:
    """One loop-local lock with a protected waiter/user count."""

    lock: asyncio.Lock
    users: int = 0


_ENTRIES_LOCK = threading.Lock()
_ENTRIES: dict[str, _LifecycleEntry] = {}


def _cross_process_lock_path(namespace: str, key: str) -> Path:
    """Return one opaque owner-private lock path for a logical resource."""
    directory = get_state_store().layout.locks_dir
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        directory.chmod(0o700)
    digest = hashlib.sha256(f"{namespace}\0{key}".encode()).hexdigest()
    return directory / f"{namespace}-{digest}.lock"


def _lifecycle_lock_path(session_id: str) -> Path:
    """Return one opaque owner-private lock path for a session id."""
    return _cross_process_lock_path("session-lifecycle", str(session_id))


class _CrossProcessLease:
    """Hold one file lock in a dedicated thread without blocking the event loop."""

    def __init__(self, path: Path, thread_name: str) -> None:
        self._path = path
        self._loop = asyncio.get_running_loop()
        self._acquired = asyncio.Event()
        self._release = threading.Event()
        self._cancel = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=thread_name,
            daemon=True,
        )

    def _run(self) -> None:
        try:
            while not self._cancel.is_set():
                try:
                    with private_file_lock(
                        self._path,
                        timeout_s=_FILE_LOCK_POLL_S,
                        retry_interval_s=min(0.01, _FILE_LOCK_POLL_S),
                    ):
                        if self._cancel.is_set():
                            return
                        self._loop.call_soon_threadsafe(self._acquired.set)
                        self._release.wait()
                        return
                except TimeoutError:
                    continue
        except BaseException as exc:
            self._error = exc
            self._loop.call_soon_threadsafe(self._acquired.set)

    async def _join_uninterruptibly(self) -> None:
        cancelled = False
        while self._thread.is_alive():
            try:
                await asyncio.sleep(_THREAD_JOIN_POLL_S)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def acquire(self) -> None:
        self._thread.start()
        try:
            await self._acquired.wait()
        except asyncio.CancelledError:
            self._cancel.set()
            self._release.set()
            await self._join_uninterruptibly()
            raise
        if self._error is not None:
            raise self._error
        if not self._thread.is_alive():
            raise RuntimeError(
                "session lifecycle lock exited before acquisition"
            )

    async def release(self) -> None:
        self._release.set()
        await self._join_uninterruptibly()
        if self._error is not None:
            raise self._error


@asynccontextmanager
async def cross_process_lock(
    namespace: str, key: str = "global"
) -> AsyncGenerator[None]:
    """Hold one cancellation-safe owner-private lock across server processes."""
    path = _cross_process_lock_path(str(namespace), str(key))
    lease = _CrossProcessLease(path, f"{namespace}-{key}")
    acquired = False
    try:
        await lease.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            await lease.release()


@asynccontextmanager
async def session_lifecycle_lock(session_id: str) -> AsyncGenerator[None]:
    """Serialize admission, foreground work, retry, and teardown for one session.

    Entries are removed after the last holder or waiter exits. This avoids both
    unbounded process-local state and reusing an asyncio lock from a prior event
    loop in tests or embedded runtimes. A distinct private file lock extends the
    same exclusion across server processes sharing one state directory.
    """
    with _ENTRIES_LOCK:
        entry = _ENTRIES.get(session_id)
        if entry is None:
            entry = _LifecycleEntry(lock=asyncio.Lock())
            _ENTRIES[session_id] = entry
        entry.users += 1

    acquired = False
    lease: _CrossProcessLease | None = None
    lease_acquired = False
    try:
        await entry.lock.acquire()
        acquired = True
        lease = _CrossProcessLease(
            _lifecycle_lock_path(session_id),
            f"session-lifecycle-{session_id}",
        )
        await lease.acquire()
        lease_acquired = True
        yield
    finally:
        try:
            if lease is not None and lease_acquired:
                await lease.release()
        finally:
            if acquired:
                entry.lock.release()
            with _ENTRIES_LOCK:
                entry.users -= 1
                if entry.users == 0 and _ENTRIES.get(session_id) is entry:
                    _ENTRIES.pop(session_id, None)


@asynccontextmanager
async def session_lifecycle_locks(
    session_ids: Iterable[str],
) -> AsyncGenerator[None]:
    """Acquire multiple session lifecycle locks in a stable deadlock-free order."""
    normalized = sorted({str(session_id) for session_id in session_ids})
    async with AsyncExitStack() as stack:
        for session_id in normalized:
            await stack.enter_async_context(session_lifecycle_lock(session_id))
        yield


@contextlib.contextmanager
def try_session_lifecycle_lease(session_id: str) -> Generator[bool]:
    """Try to hold one lifecycle file lease without waiting.

    Synchronous pruning cannot await the normal asyncio lifecycle lock while it
    already holds the session registry transaction. Treat local users, external
    contention, and lock-file I/O uncertainty as protected so pruning remains
    fail closed and avoids reverse-order deadlocks.
    """
    normalized = str(session_id)
    with _ENTRIES_LOCK:
        entry = _ENTRIES.get(normalized)
        if entry is not None and entry.users:
            yield False
            return

    stack = contextlib.ExitStack()
    try:
        stack.enter_context(
            private_file_lock(
                _lifecycle_lock_path(normalized),
                timeout_s=0,
                retry_interval_s=0,
            )
        )
    except OSError, TimeoutError:
        yield False
        return

    try:
        with _ENTRIES_LOCK:
            entry = _ENTRIES.get(normalized)
            if entry is not None and entry.users:
                yield False
                return
        yield True
    finally:
        stack.close()
