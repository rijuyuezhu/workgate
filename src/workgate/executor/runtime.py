"""Executor composition owner for the long-lived machine process."""

from collections.abc import AsyncGenerator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field

from ..composition.services import (
    RuntimeServiceInstallation,
    RuntimeServices,
    build_runtime_services,
    install_runtime_services,
)
from ..config.settings import Settings
from ..protocol.executor import ExecutorCommand
from ..remote_worker.dispatch import WorkerDispatcher as LegacyWorkerDispatcher
from ..terminal.runtime import TerminalRuntime, build_terminal_runtime
from .config import ExecutorConfig, resolve_executor_config
from .connection import ExecutorConnection
from .control_client import ExecutorControlClient
from .hello import build_executor_hello
from .profile import ExecutorProfileStore, executor_run_lock
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
    profile_store: ExecutorProfileStore
    """Final executor v1 connection profile stored in executor-owned state."""
    connection: ExecutorConnection | None = field(default=None, init=False)
    """Live final executor v1 reconnect loop when a final profile exists."""
    _profile_lock: ExitStack | None = field(
        default=None, init=False, repr=False
    )
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
        profile_lock = ExitStack()
        terminal_started = False
        connection: ExecutorConnection | None = None
        try:
            await self.terminal_runtime.start()
            terminal_started = True
            profile = self.profile_store.load()
            if profile is not None:
                profile_lock.enter_context(
                    executor_run_lock(self.services.state_store)
                )
                client = ExecutorControlClient(profile)
                connection = ExecutorConnection.from_client(
                    client,
                    hello_factory=lambda: build_executor_hello(self.config),
                    execute=self._execute_protocol_command,
                    max_concurrent_commands=self.config.max_concurrent_commands,
                )
                connection.start()
        except BaseException:
            if connection is not None:
                await connection.aclose()
            profile_lock.close()
            if terminal_started:
                await self.terminal_runtime.aclose()
            installation.close()
            self._closed = True
            raise
        self.connection = connection
        self._profile_lock = profile_lock
        self._installation = installation

    async def _execute_protocol_command(self, command: ExecutorCommand):
        """Adapt final v1 envelopes to the temporary executor-local dispatcher seam."""
        args = dict(command.args)
        if command.session_id is not None:
            args.setdefault("session_id", command.session_id)
        return await self.dispatcher.execute(command.op, args)

    async def aclose(self) -> None:
        """Restore prior compatibility bindings; repeated close is harmless."""
        installation = self._installation
        self._installation = None
        connection = self.connection
        self.connection = None
        profile_lock = self._profile_lock
        self._profile_lock = None
        self._closed = True
        try:
            if connection is not None:
                await connection.aclose()
        finally:
            try:
                if profile_lock is not None:
                    profile_lock.close()
            finally:
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
        profile_store=ExecutorProfileStore(services.state_store),
    )
