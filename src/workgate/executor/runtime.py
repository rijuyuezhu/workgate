"""Executor composition owner for the long-lived machine process."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from ..composition.services import (
    RuntimeServiceInstallation,
    RuntimeServices,
    build_runtime_services,
    install_runtime_services,
)
from ..config.settings import Settings
from ..remote_worker.dispatch import WorkerDispatcher as LegacyWorkerDispatcher
from ..terminal.runtime import TerminalRuntime, build_terminal_runtime
from .config import ExecutorConfig, resolve_executor_config
from .search_composition import build_executor_dispatcher_with_search


@dataclass
class ExecutorRuntime:
    """Own the executor's composed services and compatibility lifecycle."""

    config: ExecutorConfig
    """Resolved executor-owned machine authority for new composition code."""
    legacy_settings: Settings
    """Temporary monolithic settings bridge for unmigrated components."""
    services: RuntimeServices
    """Explicit shared state services owned by this executor."""
    terminal_runtime: TerminalRuntime
    """Executor-owned terminal bridge and ConPTY live state."""
    dispatcher: LegacyWorkerDispatcher
    """Legacy dispatcher with the migrated Search service already bound."""
    _installation: RuntimeServiceInstallation | None = field(
        default=None, init=False, repr=False
    )
    _closed: bool = field(default=False, init=False, repr=False)

    async def start(self) -> None:
        """Install compatibility bindings inside the executor's owning loop."""
        if self._closed:
            raise RuntimeError(
                "ExecutorRuntime cannot be restarted after close"
            )
        if self._installation is not None:
            return
        installation = install_runtime_services(self.services)
        try:
            await self.terminal_runtime.start()
        except BaseException:
            installation.close()
            self._closed = True
            raise
        self._installation = installation

    async def aclose(self) -> None:
        """Restore prior compatibility bindings; repeated close is harmless."""
        installation = self._installation
        self._installation = None
        self._closed = True
        try:
            await self.terminal_runtime.aclose()
        finally:
            if installation is not None:
                installation.close()

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[ExecutorRuntime]:
        """Run the executor ownership scope with deterministic cleanup."""
        try:
            await self.start()
            yield self
        finally:
            await self.aclose()


def build_executor_runtime(settings: Settings) -> ExecutorRuntime:
    """Construct one executor graph without installing process globals yet."""
    services = build_runtime_services(settings)
    return ExecutorRuntime(
        config=resolve_executor_config(settings),
        legacy_settings=settings,
        services=services,
        terminal_runtime=build_terminal_runtime(),
        dispatcher=build_executor_dispatcher_with_search(
            settings, services.tool_session_store
        ),
    )
