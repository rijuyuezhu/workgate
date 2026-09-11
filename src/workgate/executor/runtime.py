"""Executor composition owner for the long-lived machine process."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..config.settings import Settings
from ..protocol.executor import (
    SESSION_CHANGE_CWD_OP,
    SESSION_CREATE_OP,
    SESSION_LOOKUP_OP,
    SESSION_TERMINATE_OP,
)
from ..utils.path_policy import resolve_path_with_policy
from .agent import ExecutorAgentBridgeService
from .config import ExecutorConfig, resolve_executor_config
from .dispatch import ExecutorDispatcher
from .files import files_config_from_settings
from .search_composition import build_executor_dispatcher_with_search
from .services import (
    RuntimeServiceInstallation,
    RuntimeServices,
    build_runtime_services,
    install_runtime_services,
)
from .shell_service import ShellService
from .terminal.runtime import TerminalRuntime, build_terminal_runtime
from .ui_files import UiFilesService
from .ui_terminals import UiTerminalsService

if TYPE_CHECKING:
    from ..protocol.executor import ExecutorCommand
    from .connection import ExecutorConnection
    from .profile import ExecutorProfileStore
    from .sessions import ExecutorSessionService


@dataclass
class ExecutorRuntime:
    """Own the executor's composed services and compatibility lifecycle."""

    config: ExecutorConfig
    """Resolved executor-owned machine authority for new composition code."""
    legacy_settings: Settings
    """Temporary monolithic settings bridge for unmigrated components."""
    services: RuntimeServices
    """Explicit shared state services owned by this executor."""
    agent_bridge: ExecutorAgentBridgeService
    """Executor-owned stdio Agent Bridge integration and credential boundary."""
    terminal_runtime: TerminalRuntime
    """Executor-owned terminal bridge and ConPTY live state."""
    dispatcher: ExecutorDispatcher
    """Executor-local machine operation dispatcher."""
    sessions: ExecutorSessionService
    """Executor-authoritative final shared-session resource service."""
    ui_files: UiFilesService
    """Executor-owned internal Human UI file operations."""
    ui_terminals: UiTerminalsService
    """Executor-owned internal Human UI terminal operations."""
    profile_store: ExecutorProfileStore | None
    """Final v1 profile store, absent for the temporary legacy worker runtime."""
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
            profile_store = self.profile_store
            profile = None if profile_store is None else profile_store.load()
            if profile is not None:
                from .connection import ExecutorConnection
                from .control_client import ExecutorControlClient
                from .hello import build_executor_hello
                from .profile import executor_run_lock

                profile_lock.enter_context(
                    executor_run_lock(self.services.state_store)
                )
                client = ExecutorControlClient(profile)
                connection = ExecutorConnection.from_client(
                    client,
                    hello_factory=lambda: build_executor_hello(
                        self.config, sessions=self.sessions.inventory()
                    ),
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
        """Adapt final v1 envelopes to executor-owned operation services."""
        ui_legacy_aliases = {
            "ui.dashboard.snapshot": "dashboard_snapshot",
        }
        legacy_ui_tool = ui_legacy_aliases.get(command.op)
        if legacy_ui_tool is not None:
            if command.session_id is not None:
                raise ValueError(f"{command.op} must not carry session_id")
            return await self.dispatcher.execute(
                legacy_ui_tool, dict(command.args)
            )
        if command.op.startswith("ui.files."):
            if command.session_id is not None:
                raise ValueError(
                    "internal UI file operations must not carry session_id"
                )
            return await self.ui_files.execute(command.op, dict(command.args))
        if command.op.startswith("ui.terminals."):
            if command.session_id is not None:
                raise ValueError(
                    "internal UI terminal operations must not carry session_id"
                )
            return await self.ui_terminals.execute(
                command.op, dict(command.args)
            )
        if command.op in {
            SESSION_CREATE_OP,
            SESSION_LOOKUP_OP,
            SESSION_TERMINATE_OP,
            SESSION_CHANGE_CWD_OP,
        }:
            if command.session_id is None:
                raise ValueError(f"{command.op} requires session_id")
            session_id = str(command.session_id)
            if command.op == SESSION_CREATE_OP:
                workdir = command.args.get("workdir")
                label = command.args.get("label")
                if not isinstance(workdir, str) or not workdir:
                    raise ValueError("session.create requires workdir")
                if label is not None and not isinstance(label, str):
                    raise ValueError(
                        "session.create label must be a string or null"
                    )
                return await self.sessions.create(
                    session_id,
                    workdir=workdir,
                    label=label,
                )
            if command.op == SESSION_LOOKUP_OP:
                return self.sessions.lookup(session_id)
            if command.op == SESSION_TERMINATE_OP:
                return await self.sessions.terminate(session_id)
            workdir = command.args.get("workdir")
            if not isinstance(workdir, str) or not workdir:
                raise ValueError("session.change_cwd requires workdir")
            return await self.sessions.change_cwd(session_id, workdir)

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
                    try:
                        self.agent_bridge.close()
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


def build_executor_runtime(
    settings: Settings, *, enable_control_connection: bool = True
) -> ExecutorRuntime:
    """Construct one executor graph without installing process globals yet."""
    config = resolve_executor_config(settings)

    def executor_path_resolver(
        path: str | Path,
        *,
        must_exist: bool = False,
        allow_missing_parent: bool = True,
        follow_final_symlink: bool = True,
    ) -> Path:
        return resolve_path_with_policy(
            path,
            workspace_root=config.workspace_root,
            allow_full_control=config.allow_full_control,
            path_denylist=config.path_denylist,
            must_exist=must_exist,
            allow_missing_parent=allow_missing_parent,
            follow_final_symlink=follow_final_symlink,
        )

    services = build_runtime_services(
        settings, path_resolver=executor_path_resolver
    )
    profile_store = None
    if enable_control_connection:
        from .profile import ExecutorProfileStore

        profile_store = ExecutorProfileStore(services.state_store)
    from .sessions import ExecutorSessionService

    shell_service = ShellService(config, services.tool_session_store)
    agent_bridge = ExecutorAgentBridgeService(config)
    return ExecutorRuntime(
        config=config,
        legacy_settings=settings,
        services=services,
        agent_bridge=agent_bridge,
        terminal_runtime=build_terminal_runtime(
            services.state_store,
            workspace_root=config.workspace_root,
            idle_timeout_s=config.ui_terminal_idle_timeout_s,
            max_connections=config.ui_terminal_max_connections,
        ),
        dispatcher=build_executor_dispatcher_with_search(
            settings,
            services.tool_session_store,
            shell_service=shell_service,
            agent_bridge_service=agent_bridge,
        ),
        sessions=ExecutorSessionService(
            config, services.tool_session_store, shell_service
        ),
        ui_files=UiFilesService(
            files_config_from_settings(settings), services.tool_session_store
        ),
        ui_terminals=UiTerminalsService(shell_service),
        profile_store=profile_store,
    )
