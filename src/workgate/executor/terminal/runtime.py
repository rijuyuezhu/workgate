"""Lifecycle owner for terminal bridge and ConPTY live process registries."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ...persistence import StateStore
from .bridge import (
    TerminalBridgeRegistry,
    configure_terminal_bridge_registry,
)
from .conpty import ConPtyRegistry, configure_conpty_registry


@dataclass
class TerminalRuntime:
    """Own terminal live state for one long-lived executor process."""

    conpty: ConPtyRegistry
    """ConPTY persistent-shell state owned by this executor runtime."""
    bridges: TerminalBridgeRegistry = field(
        default_factory=TerminalBridgeRegistry
    )
    """Raw-terminal bridge state owned by this runtime."""
    _previous_bridges: TerminalBridgeRegistry | None = field(
        default=None, init=False, repr=False
    )
    _previous_conpty: ConPtyRegistry | None = field(
        default=None, init=False, repr=False
    )
    _bindings_installed: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def start(self) -> None:
        """Bind terminal registries to this runtime's owning event loop."""
        if self._closed:
            raise RuntimeError(
                "TerminalRuntime cannot be restarted after close"
            )
        if self._bindings_installed:
            return

        conpty_started = False
        bridges_started = False
        previous_conpty: ConPtyRegistry | None = None
        conpty_bound = False
        try:
            await self.conpty.start()
            conpty_started = True
            await self.bridges.start()
            bridges_started = True
            previous_conpty = configure_conpty_registry(self.conpty)
            conpty_bound = True
            previous_bridges = configure_terminal_bridge_registry(self.bridges)
        except BaseException:
            if conpty_bound:
                configure_conpty_registry(previous_conpty)
            try:
                if bridges_started:
                    await self.bridges.aclose()
            finally:
                if conpty_started:
                    await self.conpty.aclose()
                self._closed = True
            raise

        self._previous_conpty = previous_conpty
        self._previous_bridges = previous_bridges
        self._bindings_installed = True

    def stop_admission(self) -> None:
        """Reject new terminal work before dependent shutdown begins."""
        self.bridges.stop_admission()
        self.conpty.stop_admission()

    async def aclose(self) -> None:
        """Close bridge dependents before ConPTY shells and restore bindings."""
        self._closed = True
        self.stop_admission()
        bridge_error: BaseException | None = None
        conpty_error: BaseException | None = None
        try:
            try:
                await self.bridges.aclose()
            except BaseException as exc:
                bridge_error = exc
            try:
                await self.conpty.aclose()
            except BaseException as exc:
                conpty_error = exc
        finally:
            if self._bindings_installed:
                configure_terminal_bridge_registry(self._previous_bridges)
                configure_conpty_registry(self._previous_conpty)
                self._bindings_installed = False
                self._previous_bridges = None
                self._previous_conpty = None

        if bridge_error is not None:
            raise bridge_error
        if conpty_error is not None:
            raise conpty_error

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[TerminalRuntime]:
        """Run one explicit terminal ownership scope."""
        try:
            await self.start()
            yield self
        finally:
            await self.aclose()


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
