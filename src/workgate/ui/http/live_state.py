"""Lifecycle ownership for controller-side Human UI live connection state."""

import asyncio
import itertools
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any


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


@dataclass
class HumanUiRuntime:
    """Own controller-side Human UI connection admission state."""

    terminal_connections: UiTerminalConnectionRegistry
    _previous: HumanUiRuntime | None = field(
        default=None, init=False, repr=False
    )
    _binding_installed: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def start(self) -> None:
        """Start UI admission state and install its non-owning compatibility binding."""
        if self._closed:
            raise RuntimeError("HumanUiRuntime cannot be restarted after close")
        if self._binding_installed:
            return
        started = False
        try:
            await self.terminal_connections.start()
            started = True
            previous = configure_human_ui_runtime(self)
        except BaseException:
            if started:
                await self.terminal_connections.aclose()
            self._closed = True
            raise
        self._previous = previous
        self._binding_installed = True

    def stop_admission(self) -> None:
        """Stop Human UI connections from accepting new work."""
        self.terminal_connections.stop_admission()

    async def aclose(self) -> None:
        """Close terminal connections and restore the compatibility binding."""
        self._closed = True
        self.stop_admission()
        try:
            await self.terminal_connections.aclose()
        finally:
            if self._binding_installed:
                configure_human_ui_runtime(self._previous)
                self._binding_installed = False
                self._previous = None

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


def build_human_ui_runtime() -> HumanUiRuntime:
    """Construct fresh controller-side Human UI live state."""
    return HumanUiRuntime(terminal_connections=UiTerminalConnectionRegistry())
