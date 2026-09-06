"""Lifecycle ownership for mutable Human UI terminal and remote-file state."""

import asyncio
import hashlib
import itertools
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from ...ops.utils.remote_session import (
    RemoteWorkerCall,
    call_remote_worker_tool,
    end_worker_session,
)

UI_REMOTE_FILE_LOCK_SHARDS = 64


@dataclass(frozen=True)
class _RemoteFileSession:
    """One worker session created specifically for the Human UI Files surface."""

    machine: str
    workdir: str
    worker_session_id: str


class UiTerminalConnectionRegistry:
    """Own active Human UI terminal connection admission for one controller."""

    def __init__(self) -> None:
        self._connection_ids = itertools.count(1)
        self._active: dict[int, asyncio.Task[Any] | None] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    async def start(self) -> None:
        """Bind active connection tasks to the controller's owning event loop."""
        if self._closed:
            raise RuntimeError(
                "Human UI terminal connection registry is closed"
            )
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError(
                "Human UI terminal connection registry cannot span event loops"
            )
        self._loop = loop

    def reserve(self, maximum: int) -> int | None:
        """Reserve one bounded connection slot, or return ``None`` when unavailable."""
        try:
            task = asyncio.current_task()
            loop = asyncio.get_running_loop()
        except RuntimeError:
            task = None
            loop = None
        with self._lock:
            if self._closed or len(self._active) >= maximum:
                return None
            if (
                self._loop is not None
                and loop is not None
                and self._loop is not loop
            ):
                raise RuntimeError(
                    "Human UI terminal connection registry cannot span event loops"
                )
            marker = next(self._connection_ids)
            self._active[marker] = task
            return marker

    def release(self, marker: int) -> None:
        """Release one previously reserved connection slot."""
        with self._lock:
            self._active.pop(marker, None)

    def stop_admission(self) -> None:
        """Reject new WebSocket connections before controller shutdown drains work."""
        self._closed = True

    def active_count(self) -> int:
        """Return the number of currently reserved terminal connection slots."""
        with self._lock:
            return len(self._active)

    async def aclose(self) -> None:
        """Cancel active WebSocket owner tasks and clear all connection slots."""
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError(
                "Human UI terminal connection registry must close on its owning event loop"
            )
        self.stop_admission()
        current = asyncio.current_task()
        with self._lock:
            tasks = tuple(
                {
                    task
                    for task in self._active.values()
                    if task is not None
                    and task is not current
                    and not task.done()
                }
            )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with self._lock:
            self._active.clear()


class UiRemoteFileSessionRegistry:
    """Own cached worker sessions and request admission for Human UI remote files."""

    def __init__(self, call_worker: RemoteWorkerCall) -> None:
        self.call_worker = call_worker
        self._sessions: dict[str, _RemoteFileSession] = {}
        self._retired_sessions: set[_RemoteFileSession] = set()
        self._lock = threading.Lock()
        self._machine_locks = tuple(
            threading.Lock() for _ in range(UI_REMOTE_FILE_LOCK_SHARDS)
        )
        self._operations: dict[asyncio.Task[Any], int] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    async def start(self) -> None:
        """Bind tracked remote-file operations to the owning controller loop."""
        if self._closed:
            raise RuntimeError(
                "Human UI remote-file session registry is closed"
            )
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError(
                "Human UI remote-file session registry cannot span event loops"
            )
        self._loop = loop

    def require_open(self) -> None:
        """Reject new remote-file work after shutdown admission has stopped."""
        if self._closed:
            raise RuntimeError(
                "Human UI remote-file session registry is closed"
            )

    def begin_operation(self) -> asyncio.Task[Any] | None:
        """Track one in-flight remote-file request on the owning event loop."""
        self.require_open()
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError(
                "Human UI remote-file session registry cannot span event loops"
            )
        task = asyncio.current_task()
        if task is not None:
            self._operations[task] = self._operations.get(task, 0) + 1
        return task

    def end_operation(self, task: asyncio.Task[Any] | None) -> None:
        """Release one nested operation reference for a request task."""
        if task is None:
            return
        count = self._operations.get(task, 0)
        if count <= 1:
            self._operations.pop(task, None)
        else:
            self._operations[task] = count - 1

    def stop_admission(self) -> None:
        """Reject new remote-file requests before shutdown drains active work."""
        self._closed = True

    def machine_lock(self, machine: str) -> threading.Lock:
        """Return the stable lock shard serializing one worker's session creation."""
        digest = hashlib.sha256(machine.encode("utf-8")).digest()
        shard = int.from_bytes(digest[:2], "big") % len(self._machine_locks)
        return self._machine_locks[shard]

    def discard_machine(self, machine: str) -> None:
        """Forget a cached session when its worker is unavailable or unknown."""
        with self._lock:
            self._sessions.pop(machine, None)

    def cached(self, machine: str, workdir: str) -> _RemoteFileSession | None:
        """Return a cached worker session only when its workdir is still current."""
        with self._lock:
            cached = self._sessions.get(machine)
            if cached is None or cached.workdir != workdir:
                return None
            return cached

    def store(self, session: _RemoteFileSession) -> None:
        """Cache a worker session and retain replaced sessions for shutdown cleanup."""
        self.require_open()
        with self._lock:
            self.require_open()
            previous = self._sessions.get(session.machine)
            if previous is not None and previous != session:
                self._retired_sessions.add(previous)
            self._sessions[session.machine] = session

    def invalidate(self, machine: str, worker_session_id: str) -> None:
        """Forget a cached session only when the worker id still matches."""
        with self._lock:
            cached = self._sessions.get(machine)
            if (
                cached is not None
                and cached.worker_session_id == worker_session_id
            ):
                self._sessions.pop(machine, None)

    def snapshot(self) -> tuple[_RemoteFileSession, ...]:
        """Return all worker sessions still owned by this registry."""
        with self._lock:
            return tuple((*self._sessions.values(), *self._retired_sessions))

    async def aclose(self) -> None:
        """Cancel active requests, end owned worker sessions, and clear the cache."""
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError(
                "Human UI remote-file session registry must close on its owning event loop"
            )
        self.stop_admission()
        current = asyncio.current_task()
        operations = tuple(
            task
            for task in self._operations
            if task is not current and not task.done()
        )
        for task in operations:
            task.cancel()
        if operations:
            await asyncio.gather(*operations, return_exceptions=True)
        self._operations.clear()

        with self._lock:
            sessions = tuple(
                (*self._sessions.values(), *self._retired_sessions)
            )
            self._sessions.clear()
            self._retired_sessions.clear()
        if not sessions:
            return
        results = await asyncio.gather(
            *(
                end_worker_session(
                    machine=session.machine,
                    worker_session_id=session.worker_session_id,
                    call_worker=self.call_worker,
                )
                for session in sessions
            ),
            return_exceptions=True,
        )
        errors = [
            result for result in results if isinstance(result, BaseException)
        ]
        if errors:
            raise RuntimeError(
                f"failed to end {len(errors)} Human UI remote-file session(s)"
            ) from errors[0]


@dataclass
class HumanUiRuntime:
    """Own Human UI live connection and remote-file session state."""

    terminal_connections: UiTerminalConnectionRegistry
    """Controller-owned active terminal WebSocket admission and tasks."""
    remote_files: UiRemoteFileSessionRegistry
    """Controller-owned Human UI remote-file worker sessions."""
    _previous: HumanUiRuntime | None = field(
        default=None, init=False, repr=False
    )
    _binding_installed: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def start(self) -> None:
        """Start UI registries and install their non-owning compatibility binding."""
        if self._closed:
            raise RuntimeError("HumanUiRuntime cannot be restarted after close")
        if self._binding_installed:
            return
        terminals_started = False
        remote_files_started = False
        try:
            await self.terminal_connections.start()
            terminals_started = True
            await self.remote_files.start()
            remote_files_started = True
            previous = configure_human_ui_runtime(self)
        except BaseException:
            try:
                if remote_files_started:
                    await self.remote_files.aclose()
            finally:
                if terminals_started:
                    await self.terminal_connections.aclose()
                self._closed = True
            raise
        self._previous = previous
        self._binding_installed = True

    def stop_admission(self) -> None:
        """Stop both Human UI live-state domains from accepting new work."""
        self.terminal_connections.stop_admission()
        self.remote_files.stop_admission()

    async def aclose(self) -> None:
        """Close terminal connections before remote-file sessions and restore binding."""
        self._closed = True
        self.stop_admission()
        terminal_error: BaseException | None = None
        remote_file_error: BaseException | None = None
        try:
            try:
                await self.terminal_connections.aclose()
            except BaseException as exc:
                terminal_error = exc
            try:
                await self.remote_files.aclose()
            except BaseException as exc:
                remote_file_error = exc
        finally:
            if self._binding_installed:
                configure_human_ui_runtime(self._previous)
                self._binding_installed = False
                self._previous = None
        if terminal_error is not None:
            raise terminal_error
        if remote_file_error is not None:
            raise remote_file_error

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[HumanUiRuntime]:
        """Run one explicit Human UI live-state ownership scope."""
        try:
            await self.start()
            yield self
        finally:
            await self.aclose()


_HUMAN_UI_RUNTIME: HumanUiRuntime | None = None


def configure_human_ui_runtime(
    runtime: HumanUiRuntime | None,
) -> HumanUiRuntime | None:
    """Install a non-owning compatibility binding and return the previous runtime."""
    global _HUMAN_UI_RUNTIME
    previous = _HUMAN_UI_RUNTIME
    _HUMAN_UI_RUNTIME = runtime
    return previous


def human_ui_runtime() -> HumanUiRuntime:
    """Return the currently bound Human UI owner or fail outside its lifespan."""
    runtime = _HUMAN_UI_RUNTIME
    if runtime is None:
        raise RuntimeError(
            "Human UI runtime is not configured; start ControlRuntime"
        )
    return runtime


def build_human_ui_runtime(
    call_worker: RemoteWorkerCall | None = None,
) -> HumanUiRuntime:
    """Construct fresh Human UI live-state registries without installing them."""
    worker_call = call_worker or call_remote_worker_tool
    return HumanUiRuntime(
        terminal_connections=UiTerminalConnectionRegistry(),
        remote_files=UiRemoteFileSessionRegistry(worker_call),
    )
