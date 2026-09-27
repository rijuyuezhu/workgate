"""Lifecycle owner for terminal bridge and ConPTY live process registries."""

from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ...persistence import StateStore
from .bridge import TerminalBridgeRegistry, use_terminal_bridge_registry
from .conpty import ConPtyRegistry, use_conpty_registry


@dataclass
class TerminalRuntime:
    """Own terminal live state for one long-lived executor process."""

    conpty: ConPtyRegistry
    """ConPTY persistent-shell state owned by this executor runtime."""
    bridges: TerminalBridgeRegistry = field(
        default_factory=TerminalBridgeRegistry
    )
    """Raw-terminal bridge state owned by this runtime."""
    _started: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _close_complete: bool = field(default=False, init=False, repr=False)

    async def start(self) -> None:
        """Start terminal registries owned by this runtime."""
        if self._closed:
            raise RuntimeError(
                "TerminalRuntime cannot be restarted after close"
            )
        if self._started:
            return

        conpty_started = False
        try:
            await self.conpty.start()
            conpty_started = True
            await self.bridges.start()
        except BaseException:
            if conpty_started:
                await self.conpty.aclose()
            self._closed = True
            raise
        self._started = True

    def stop_admission(self) -> None:
        """Reject new terminal work before dependent shutdown begins."""
        self.bridges.stop_admission()
        self.conpty.stop_admission()

    async def aclose(self) -> None:
        """Close bridge dependents before ConPTY shells."""
        if self._close_complete:
            return
        self._closed = True
        self._started = False
        self.stop_admission()
        bridge_error: BaseException | None = None
        conpty_error: BaseException | None = None
        try:
            await self.bridges.aclose()
        except BaseException as exc:
            bridge_error = exc
        try:
            await self.conpty.aclose()
        except BaseException as exc:
            conpty_error = exc

        if bridge_error is not None:
            raise bridge_error
        if conpty_error is not None:
            raise conpty_error
        self._close_complete = True

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[TerminalRuntime]:
        """Run one explicit terminal ownership scope."""
        try:
            await self.start()
            yield self
        finally:
            await self.aclose()


@contextmanager
def use_terminal_runtime(runtime: TerminalRuntime) -> Generator[None]:
    """Bind one executor terminal runtime to the current execution context."""
    with (
        use_conpty_registry(runtime.conpty),
        use_terminal_bridge_registry(runtime.bridges),
    ):
        yield


def build_terminal_runtime(
    state_store: StateStore,
    *,
    workspace_root: Path,
    idle_timeout_s: int = 300,
    max_connections: int = 8,
) -> TerminalRuntime:
    """Construct fresh executor-owned terminal live-state registries."""
    return TerminalRuntime(
        conpty=ConPtyRegistry(state_store, workspace_root),
        bridges=TerminalBridgeRegistry(
            idle_timeout_s=idle_timeout_s,
            max_connections=max_connections,
        ),
    )
