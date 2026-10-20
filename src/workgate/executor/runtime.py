"""Executor composition owner for the long-lived machine process."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import JsonValue
from websockets.exceptions import ConnectionClosedOK

from ..config.executor import ExecutorConfig
from ..config.role_config import use_role_config
from ..persistence import use_state_store
from ..protocol.executor import (
    SESSION_CHANGE_WORKDIR_OP,
    SESSION_CREATE_OP,
    SESSION_LOOKUP_OP,
    SESSION_TERMINATE_OP,
    ExecutorRuntimeOwnership,
)
from .agent import ExecutorAgentBridgeService
from .browser import BrowserService
from .dispatch import ExecutorDispatcher
from .errors import ExecutorResourceInventoryUnavailable
from .files import files_config_from_executor_config
from .gui import GuiService, build_gui_service
from .path import resolve_default_workdir, resolve_path
from .peer_probe import PeerProbeManager, connect_peer_probe
from .peer_receiver import PeerReceiverManager
from .peer_sender import send_peer_transfer
from .services import RuntimeServices, build_runtime_services
from .shell_service import ShellService
from .terminal.runtime import (
    TerminalRuntime,
    build_terminal_runtime,
    use_terminal_runtime,
)
from .tool_composition import build_executor_tool_dispatcher
from .ui_files import UiFilesService
from .ui_terminals import UiTerminalsService

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..protocol.executor import (
        ExecutorCommand,
        ExecutorHelloRequest,
        ExecutorHelloResponse,
    )
    from .connection import ExecutorConnection, RuntimePolicyAction
    from .profile import ExecutorProfileStore
    from .runtime_update import ExecutorRuntimeState, ExecutorRuntimeStateStore
    from .sessions import ExecutorSessionService


def _peer_arg[T: (str, int, bool)](
    args: dict[str, JsonValue], key: str, expected_type: type[T]
) -> T:
    """Peer RPC metadata must use exact JSON types, without coercion."""
    value = args.get(key)
    if type(value) is not expected_type:
        raise ValueError(f"peer {key} must be {expected_type.__name__}")
    return cast(T, value)


@dataclass
class ExecutorRuntime:
    """Own the executor process's composed services and lifecycle."""

    config: ExecutorConfig
    """Resolved executor-owned machine authority."""
    services: RuntimeServices
    """Explicit shared state services owned by this executor."""
    agent_bridge: ExecutorAgentBridgeService
    """Executor-owned stdio Agent Bridge integration and credential boundary."""
    terminal_runtime: TerminalRuntime
    """Executor-owned raw-terminal bridge lifecycle."""
    dispatcher: ExecutorDispatcher
    """Executor-local machine operation dispatcher."""
    shell: ShellService
    """Executor-owned public shell and tracked-job resource service."""
    sessions: ExecutorSessionService
    """Executor-authoritative execution-session resource service."""
    ui_files: UiFilesService
    """Executor-owned internal WebUI file operations."""
    ui_terminals: UiTerminalsService
    """Executor-owned internal WebUI terminal operations."""
    profile_store: ExecutorProfileStore | None
    """Persistent executor profile store, when configured."""
    browser: BrowserService
    """Executor-owned ephemeral structured browser resources."""
    gui: GuiService
    """Executor-owned short-lived native desktop GUI observations."""
    peer_receiver: PeerReceiverManager
    """One-time trusted-overlay transfer listeners."""
    peer_probe: PeerProbeManager
    """Sessionless, one-use bounded connectivity-check listeners."""
    runtime_state_store: ExecutorRuntimeStateStore
    """Small persisted runtime ownership/update authority."""
    managed_service: bool = False
    """Whether this process is owned by the native executor service manager."""
    runtime_ownership: ExecutorRuntimeOwnership | None = None
    """Install provenance supplied by the managed-service definition."""
    connection: ExecutorConnection | None = field(default=None, init=False)
    """Live control reconnect loop when a profile exists."""
    _terminal_stream_tasks: set[asyncio.Task[None]] = field(
        default_factory=set, init=False, repr=False
    )
    _profile_lock: ExitStack | None = field(
        default=None, init=False, repr=False
    )
    _started: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def _runtime_state(self) -> ExecutorRuntimeState:
        from .runtime_update import (
            ExecutorRuntimeState,
            detect_runtime_ownership,
        )

        if self.managed_service:
            return self.runtime_state_store.service_runtime_state(
                self.runtime_ownership
            )
        return ExecutorRuntimeState(ownership=detect_runtime_ownership())

    async def _build_reconnect_hello(self) -> ExecutorHelloRequest:
        """Build one complete authoritative executor resource inventory."""
        from .hello import build_executor_hello
        from .overlay import detect_tailscale_overlay

        runtime_state = self._runtime_state()
        overlay = await asyncio.to_thread(detect_tailscale_overlay)

        with (
            use_role_config(self.config),
            use_state_store(self.services.state_store),
            use_terminal_runtime(self.terminal_runtime),
        ):
            for _attempt in range(2):
                before = self.sessions.inventory()
                session_ids = frozenset(str(row.session_id) for row in before)
                shells, jobs = await self.shell.reconnect_inventory(session_ids)
                sessions = self.sessions.inventory()
                if (
                    frozenset(str(row.session_id) for row in sessions)
                    == session_ids
                ):
                    return build_executor_hello(
                        self.config,
                        sessions=sessions,
                        shells=shells,
                        jobs=jobs,
                        runtime_ownership=runtime_state.ownership,
                        overlay=overlay,
                    )
        raise ExecutorResourceInventoryUnavailable(
            "session inventory changed while reconnect snapshot was built"
        )

    async def _handle_runtime_policy(
        self, policy: ExecutorHelloResponse
    ) -> RuntimePolicyAction:
        """Apply exact-version policy without mutating externally owned installs."""
        from ..protocol.executor import ExecutorRuntimeOwnership
        from .connection import RuntimePolicyAction
        from .runtime_update import (
            ExecutorRuntimeUpdateStatus,
            apply_control_managed_update,
        )

        state = self._runtime_state()
        if not policy.runtime_update_required:
            if self.managed_service:
                self.runtime_state_store.set_update(
                    ExecutorRuntimeUpdateStatus.IDLE
                )
            return RuntimePolicyAction.CONTINUE

        target = policy.required_workgate_version
        if state.ownership is not ExecutorRuntimeOwnership.CONTROL_MANAGED:
            error = RuntimeError(
                f"control requires Workgate {target}, but runtime ownership is "
                f"{state.ownership.value}; use the installation owner to update it"
            )
            if self.managed_service:
                self.runtime_state_store.set_update(
                    ExecutorRuntimeUpdateStatus.REQUIRED,
                    target_version=target,
                    detail=str(error),
                )
                return RuntimePolicyAction.BLOCK
            raise error
        if not self.managed_service:
            raise RuntimeError(
                "control-managed updates require the installed executor service"
            )

        profile_store = self.profile_store
        if profile_store is None:
            raise RuntimeError("executor profile store is unavailable")
        profile = profile_store.load()
        if profile is None:
            raise RuntimeError("executor profile is unavailable")
        from ..platform.host import HostOS, host_os
        from .service import ExecutorServiceManager

        windows_activate = (
            ExecutorServiceManager(self.config).activate_windows_runtime
            if host_os() is HostOS.WINDOWS
            else None
        )
        try:
            await apply_control_managed_update(
                control_url=profile.control_url,
                target_version=target,
                store=self.runtime_state_store,
                windows_activate=windows_activate,
            )
        except Exception:
            return RuntimePolicyAction.BLOCK
        return RuntimePolicyAction.RESTART

    async def start(self) -> None:
        """Start resources owned by this executor runtime."""
        if self._closed:
            raise RuntimeError(
                "ExecutorRuntime cannot be restarted after close"
            )
        if self._started:
            return
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
                from .profile import executor_run_lock

                profile_lock.enter_context(
                    executor_run_lock(self.services.state_store)
                )
                client = ExecutorControlClient(profile)
                connection = ExecutorConnection.from_client(
                    client,
                    hello_factory=self._build_reconnect_hello,
                    execute=self._execute_protocol_command,
                    max_concurrent_commands=self.config.max_concurrent_commands,
                    runtime_policy_handler=self._handle_runtime_policy,
                )
                connection.start()
        except BaseException:
            if connection is not None:
                await connection.aclose()
            profile_lock.close()
            if terminal_started:
                await self.terminal_runtime.aclose()
            self._closed = True
            raise
        self.connection = connection
        self._profile_lock = profile_lock
        self._started = True

    async def _execute_protocol_command(self, command: ExecutorCommand) -> Any:
        """Execute one final command under this executor's state context."""
        with (
            use_role_config(self.config),
            use_state_store(self.services.state_store),
            use_terminal_runtime(self.terminal_runtime),
        ):
            return await self._execute_protocol_command_with_state(command)

    async def _execute_protocol_command_with_state(
        self, command: ExecutorCommand
    ):
        """Adapt executor protocol envelopes to owned operation services."""
        if command.op == "terminal.attach":
            if command.session_id is not None:
                raise ValueError("terminal.attach must not carry session_id")
            profile_store = self.profile_store
            profile = None if profile_store is None else profile_store.load()
            if profile is None:
                raise RuntimeError(
                    "terminal.attach requires a paired executor profile"
                )
            stream_id = command.args.get("stream_id")
            shell_id = command.args.get("shell_id")
            if not isinstance(stream_id, str) or not stream_id:
                raise ValueError("terminal.attach requires stream_id")
            if not isinstance(shell_id, str) or not shell_id:
                raise ValueError("terminal.attach requires shell_id")
            cols_arg = command.args.get("cols")
            rows_arg = command.args.get("rows")
            if cols_arg is not None and not isinstance(
                cols_arg, (str, int, float)
            ):
                raise ValueError("terminal.attach cols must be numeric")
            if rows_arg is not None and not isinstance(
                rows_arg, (str, int, float)
            ):
                raise ValueError("terminal.attach rows must be numeric")
            from .terminal.stream import connect_executor_terminal_stream

            stream = await connect_executor_terminal_stream(
                profile,
                stream_id=stream_id,
                shell_id=shell_id,
                cols=120 if cols_arg is None else int(cols_arg),
                rows=36 if rows_arg is None else int(rows_arg),
            )
            task = asyncio.create_task(stream.run())
            self._terminal_stream_tasks.add(task)
            task.add_done_callback(
                lambda done: self._terminal_stream_done(
                    done, stream_id=stream.stream_id
                )
            )
            return {
                "stream_id": stream.stream_id,
                "shell_id": stream.shell_id,
                "backend": stream.backend,
                "connected": True,
            }
        if (
            command.op == "dashboard_snapshot"
            and command.session_id is not None
        ):
            raise ValueError("dashboard_snapshot must not carry session_id")
        if command.op == "ui.config.save":
            if command.session_id is not None or set(command.args) != {
                "revision",
                "changes",
            }:
                raise ValueError("Invalid config save request")
            from ..config.editing import ConfigEditError, save_config
            from ..config.inspection import current_startup_config

            snapshot = current_startup_config("executor")
            if snapshot is None:
                raise RuntimeError(
                    "Executor startup configuration is unavailable"
                )
            revision = command.args["revision"]
            changes = command.args["changes"]
            if not isinstance(revision, str) or not isinstance(changes, dict):
                raise ValueError("Invalid config save request")
            try:
                return {
                    "revision": await asyncio.to_thread(
                        save_config,
                        snapshot,
                        revision=revision,
                        changes=changes,
                    )
                }
            except ConfigEditError as exc:
                return {"error": exc.code, "status": exc.status}
        if command.op == "ui.config.apply":
            if command.session_id is not None or set(command.args) != {
                "revision"
            }:
                raise ValueError("Invalid config apply request")
            from ..config.editing import ConfigEditError, validate_apply
            from ..config.inspection import current_startup_config
            from ..supervisor import restart_status

            snapshot = current_startup_config("executor")
            if snapshot is None:
                raise RuntimeError(
                    "Executor startup configuration is unavailable"
                )
            state = restart_status()
            if state not in {"healthy", "recovered"}:
                return {"error": "ParentUnavailable", "status": 409}
            revision = command.args["revision"]
            if not isinstance(revision, str):
                raise ValueError("Invalid config apply request")
            try:
                await asyncio.to_thread(
                    validate_apply, snapshot, revision=revision
                )
            except ConfigEditError as exc:
                return {"error": exc.code, "status": exc.status}
            # The connection submits the successful result to Control *before*
            # triggering the Parent; this handler only validates and accepts.
            return {"status": "queued"}
        if command.op == "ui.config.inspect":
            if command.session_id is not None or command.args:
                raise ValueError(
                    "config inspection cannot be session-bound or parameterized"
                )
            from ..config.inspection import current_startup_config

            snapshot = current_startup_config("executor")
            if snapshot is None:
                raise RuntimeError(
                    "Executor startup configuration is unavailable"
                )
            from ..supervisor import restart_status

            data = await asyncio.to_thread(snapshot.describe)
            return {**data, "apply_status": restart_status()}
        if command.op == "ui.executor_diagnostics":
            if command.session_id is not None or command.args:
                raise ValueError(
                    "executor diagnostics accept no Session or arguments"
                )
            from .diagnostics import executor_diagnostics

            return await asyncio.to_thread(executor_diagnostics, self.config)
        if command.op in {
            "ui.mcp.auth",
            "ui.mcp.manage",
            "ui.mcp.test",
            "ui.skill.manage",
            "ui.skill.files",
        }:
            if command.session_id is not None:
                raise ValueError(
                    "Executor administration cannot be session-bound"
                )
            from .integration_admin import manage_executor_integration

            return await manage_executor_integration(
                self.config, command.op, command.args
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
            SESSION_CHANGE_WORKDIR_OP,
        }:
            if command.session_id is None:
                raise ValueError(f"{command.op} requires session_id")
            session_id = str(command.session_id)
            if command.op == SESSION_CREATE_OP:
                workdir = command.args.get("workdir")
                label = command.args.get("label")
                if workdir is not None and (
                    not isinstance(workdir, str) or not workdir
                ):
                    raise ValueError(
                        "session.create workdir must be a non-empty string or null"
                    )
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
                async with self.peer_receiver.session_lifecycle(session_id):
                    return await self.sessions.terminate(session_id)
            workdir = command.args.get("workdir")
            if not isinstance(workdir, str) or not workdir:
                raise ValueError("session.change_workdir requires workdir")
            async with self.peer_receiver.session_lifecycle(session_id):
                return await self.sessions.change_workdir(session_id, workdir)

        args = dict(command.args)
        if command.session_id is not None:
            args.setdefault("session_id", command.session_id)
        if command.op.startswith("peer.probe_"):
            if command.session_id is not None:
                raise ValueError(
                    "peer connectivity diagnostics cannot carry session_id"
                )
            if command.op == "peer.probe_prepare":
                return await self.peer_probe.prepare(
                    _peer_arg(args, "tailnet_id", str)
                )
            if command.op == "peer.probe_cancel":
                await self.peer_probe.cancel(_peer_arg(args, "probe_id", str))
                return {"cancelled": True}
            if command.op == "peer.probe_connect":
                return await connect_peer_probe(
                    host=_peer_arg(args, "host", str),
                    port=_peer_arg(args, "port", int),
                    token=_peer_arg(args, "token", str),
                    tailnet_id=_peer_arg(args, "tailnet_id", str),
                )
            raise ValueError("Unknown peer probe operation")
        if command.op == "transfer.peer_prepare":
            if command.session_id is None:
                raise ValueError("peer prepare requires a destination session")
            return await self.peer_receiver.prepare(
                session_id=str(command.session_id),
                path=_peer_arg(args, "path", str),
                transfer_id=_peer_arg(args, "transfer_id", str),
                expected_bytes=_peer_arg(args, "expected_bytes", int),
                expected_sha256=_peer_arg(args, "expected_sha256", str),
                overwrite=_peer_arg(args, "overwrite", bool),
                tailnet_id=_peer_arg(args, "tailnet_id", str),
            )
        if command.op == "transfer.peer_send":
            if command.session_id is None:
                raise ValueError("peer send requires a source session")
            return await send_peer_transfer(
                self.config,
                self.services.tool_session_store,
                session_id=str(command.session_id),
                path=_peer_arg(args, "path", str),
                transfer_id=_peer_arg(args, "transfer_id", str),
                host=_peer_arg(args, "host", str),
                port=_peer_arg(args, "port", int),
                token=_peer_arg(args, "token", str),
                tailnet_id=_peer_arg(args, "tailnet_id", str),
                expected_bytes=_peer_arg(args, "expected_bytes", int),
                expected_sha256=_peer_arg(args, "expected_sha256", str),
                chunk_size=(
                    _peer_arg(args, "chunk_size", int)
                    if "chunk_size" in args
                    else None
                ),
                unbound_temp=(
                    _peer_arg(args, "_workgate_unbound_temp", bool)
                    if "_workgate_unbound_temp" in args
                    else False
                ),
            )
        if command.op in {"transfer.peer_status", "transfer.peer_cancel"}:
            if command.session_id is None:
                raise ValueError(
                    "peer operation requires a destination session"
                )
            transfer_id = _peer_arg(args, "transfer_id", str)
            session_id = str(command.session_id)
            if command.op == "transfer.peer_status":
                return {
                    "state": self.peer_receiver.status(
                        transfer_id, session_id=session_id
                    )
                }
            cancelled = await self.peer_receiver.cancel(
                transfer_id,
                session_id=session_id,
                preserve_received=(
                    _peer_arg(args, "preserve_received", bool)
                    if "preserve_received" in args
                    else False
                ),
            )
            return {"cancelled": cancelled}
        if command.op in {"transfer.http_upload", "transfer.http_download"}:
            profile_store = self.profile_store
            profile = None if profile_store is None else profile_store.load()
            if profile is None:
                raise RuntimeError(
                    "raw transfer requires a paired executor profile"
                )
            from .transfer_http import (
                download_from_control,
                upload_to_control,
            )

            handler = (
                upload_to_control
                if command.op == "transfer.http_upload"
                else download_from_control
            )
            return await handler(
                profile,
                self.config,
                self.services.tool_session_store,
                args,
            )
        if command.op in {"transfer.url_upload", "transfer.url_download"}:
            from .transfer_http import download_from_url, upload_to_url

            handler = (
                upload_to_url
                if command.op == "transfer.url_upload"
                else download_from_url
            )
            return await handler(
                self.config,
                self.services.tool_session_store,
                args,
            )
        return await self.dispatcher.execute(command.op, args)

    def _terminal_stream_done(
        self, task: asyncio.Task[None], *, stream_id: str
    ) -> None:
        self._terminal_stream_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None and not isinstance(error, ConnectionClosedOK):
            # Exception messages and tracebacks can contain terminal data
            # or credentials. Only report the validated id and error type.
            logger.warning(
                "Terminal stream %s failed (%s)",
                stream_id,
                type(error).__name__,
            )

    async def aclose(self) -> None:
        """Close executor-owned live resources; failed cleanup remains retryable."""
        connection = self.connection
        self.connection = None
        profile_lock = self._profile_lock
        self._profile_lock = None
        self._closed = True
        self._started = False
        try:
            if connection is not None:
                await connection.aclose()
        finally:
            stream_tasks = tuple(self._terminal_stream_tasks)
            self._terminal_stream_tasks.clear()
            for task in stream_tasks:
                task.cancel()
            if stream_tasks:
                await asyncio.gather(*stream_tasks, return_exceptions=True)
            try:
                if profile_lock is not None:
                    profile_lock.close()
            finally:
                try:
                    try:
                        try:
                            try:
                                await self.peer_probe.aclose()
                            finally:
                                await self.peer_receiver.aclose()
                        finally:
                            await self.gui.aclose()
                    finally:
                        await self.browser.aclose()
                finally:
                    await self.terminal_runtime.aclose()

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[ExecutorRuntime]:
        """Run the executor ownership scope with deterministic cleanup."""
        try:
            await self.start()
            yield self
        finally:
            await self.aclose()


def build_executor_runtime(
    config: ExecutorConfig,
    *,
    enable_control_connection: bool = True,
    managed_service: bool = False,
    runtime_ownership: ExecutorRuntimeOwnership | None = None,
) -> ExecutorRuntime:
    """Construct one executor-owned runtime graph."""

    def executor_path_resolver(
        path: str | Path,
        *,
        must_exist: bool = False,
        allow_missing_parent: bool = True,
        follow_final_symlink: bool = True,
    ) -> Path:
        return resolve_path(
            path,
            base=resolve_default_workdir(config.default_workdir),
            must_exist=must_exist,
            allow_missing_parent=allow_missing_parent,
            follow_final_symlink=follow_final_symlink,
        )

    services = build_runtime_services(
        config, path_resolver=executor_path_resolver
    )
    profile_store = None
    if enable_control_connection:
        from .profile import ExecutorProfileStore

        profile_store = ExecutorProfileStore(services.state_store)
    from .sessions import ExecutorSessionService

    shell_service = ShellService(config, services.tool_session_store)
    agent_bridge = ExecutorAgentBridgeService(
        config, services.tool_session_store
    )
    browser_service = BrowserService(config, services.tool_session_store)
    gui_service = build_gui_service(config, services.tool_session_store)
    from .runtime_update import ExecutorRuntimeStateStore

    runtime_state_store = ExecutorRuntimeStateStore(services.state_store)
    return ExecutorRuntime(
        config=config,
        services=services,
        agent_bridge=agent_bridge,
        shell=shell_service,
        terminal_runtime=build_terminal_runtime(
            idle_timeout_s=config.ui_terminal_idle_timeout_s,
            max_connections=config.ui_terminal_max_connections,
        ),
        dispatcher=build_executor_tool_dispatcher(
            config,
            services.tool_session_store,
            shell_service=shell_service,
            agent_bridge_service=agent_bridge,
            browser_service=browser_service,
            gui_service=gui_service,
        ),
        sessions=ExecutorSessionService(
            config,
            services.tool_session_store,
            shell_service,
            browser_service,
            gui_service,
        ),
        ui_files=UiFilesService(
            files_config_from_executor_config(config),
            services.tool_session_store,
        ),
        ui_terminals=UiTerminalsService(shell_service),
        profile_store=profile_store,
        browser=browser_service,
        gui=gui_service,
        peer_receiver=PeerReceiverManager(config, services.tool_session_store),
        peer_probe=PeerProbeManager(),
        runtime_state_store=runtime_state_store,
        managed_service=managed_service,
        runtime_ownership=runtime_ownership,
    )
