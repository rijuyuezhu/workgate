"""Executor-owned lifecycle for raw terminal bridge sessions."""

import asyncio
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field

from ...platform.windows_conpty import ConPtyRegistry, use_conpty_registry
from .bridge import TerminalBridgeRegistry, use_terminal_bridge_registry


@dataclass
class TerminalRuntime:
    """Own active raw-terminal bridge sessions for one executor process."""

    bridges: TerminalBridgeRegistry = field(
        default_factory=TerminalBridgeRegistry
    )
    conpty: ConPtyRegistry = field(default_factory=ConPtyRegistry)
    _started: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _close_complete: bool = field(default=False, init=False, repr=False)

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError(
                "TerminalRuntime cannot be restarted after close"
            )
        if self._started:
            return
        try:
            await self.bridges.start()
        except BaseException:
            self._closed = True
            raise
        self._started = True

    def stop_admission(self) -> None:
        self.bridges.stop_admission()

    async def aclose(self) -> None:
        if self._close_complete:
            return
        self._closed = True
        self._started = False
        self.stop_admission()
        try:
            await self.bridges.aclose()
        finally:
            await asyncio.to_thread(self.conpty.close)
        self._close_complete = True

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[TerminalRuntime]:
        try:
            await self.start()
            yield self
        finally:
            await self.aclose()


@contextmanager
def use_terminal_runtime(runtime: TerminalRuntime) -> Generator[None]:
    with (
        use_terminal_bridge_registry(runtime.bridges),
        use_conpty_registry(runtime.conpty),
    ):
        yield


def build_terminal_runtime(
    *,
    idle_timeout_s: int = 300,
    max_connections: int = 8,
) -> TerminalRuntime:
    """Construct the executor-owned live bridge registry."""
    return TerminalRuntime(
        bridges=TerminalBridgeRegistry(
            idle_timeout_s=idle_timeout_s,
            max_connections=max_connections,
        ),
    )
