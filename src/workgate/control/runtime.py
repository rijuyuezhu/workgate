"""Control composition owner for long-lived server processes."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

from ..config.control import ControlConfig, resolve_control_config
from ..config.settings import Settings
from ..jobs.managed import ManagedJobsRuntime
from ..oauth.core.state import OAuthState, build_oauth_state
from ..persistence import FileStateStore
from ..tools.catalog import ToolCatalog
from ..ui.http.live_state import HumanUiRuntime, build_human_ui_runtime
from .audit import ControlAuditService
from .downloads import ControlDownloadService
from .executor_transport import ExecutorTransport
from .executors import ControlExecutorFleetService
from .jobs import ControlJobService
from .object_store_transfer import S3ObjectTransferService
from .pairing import ExecutorPairingService
from .payload_store import PayloadStore
from .session_copy import ControlSessionCopyService
from .sessions import ControlSessionCoordinator
from .state import ControlState
from .streams import ControlStreamHub
from .task_state import ControlTaskService
from .tool_composition import build_control_tool_catalog
from .transfer_gateway import ControlTransferGateway


@dataclass
class ControlRuntime:
    """Own the control process's composed services and lifecycle."""

    config: ControlConfig
    """Resolved control-owned authority."""
    state_store: FileStateStore
    """Control-private durable state owned by this runtime."""
    control_state: ControlState
    """Restart-critical durable control facts backed by the shared state store."""
    executor_transport: ExecutorTransport
    """Process-local ordinary RPC queues, presence, polls, and result waiters."""
    executor_pairing: ExecutorPairingService
    """Process-local device-code pairing attempts and transient credential delivery."""
    executor_fleet: ControlExecutorFleetService
    """Canonical executor discovery/admin composition over trust and live state."""
    session_coordinator: ControlSessionCoordinator
    """Control authority for execution-session lifecycle and executor routing."""
    session_copy_service: ControlSessionCopyService
    """Control orchestration for copies between existing execution sessions."""
    transfer_gateway: ControlTransferGateway
    """Process-local raw-byte capabilities bound to trusted executors."""
    download_service: ControlDownloadService
    """Control-owned public file-link snapshots sourced through executor RPC."""
    job_service: ControlJobService
    """Hybrid public job routing across executor resources and control-managed jobs."""
    task_service: ControlTaskService
    """Control-owned canonical task/progress/plan state with Todo compatibility."""
    audit_service: ControlAuditService
    """Control authority for canonical public audit history."""
    managed_jobs_runtime: ManagedJobsRuntime
    """Control-owned managed background-job tasks, handlers, and leases."""
    stream_hub: ControlStreamHub
    """Process-local terminal stream rendezvous and live relay state."""
    human_ui_runtime: HumanUiRuntime
    """Control-owned Human UI connection admission state."""
    oauth_state: OAuthState
    """Control-owned dynamic-client and authorization-code live state."""
    tool_catalog: ToolCatalog
    """Control-owned public tool catalog."""
    _started: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _close_complete: bool = field(default=False, init=False, repr=False)

    async def start(self) -> None:
        """Start services owned by this control runtime."""
        if self._closed:
            raise RuntimeError("ControlRuntime cannot be restarted after close")
        if self._started:
            return

        managed_jobs_started = False
        oauth_started = False
        human_ui_started = False
        executor_transport_started = False
        try:
            self.control_state.start()
            await self.task_service.migrate_legacy_sessions()
            self.executor_transport.start()
            executor_transport_started = True
            await self.session_copy_service.reconcile_object_store_orphans()
            await self.managed_jobs_runtime.start()
            managed_jobs_started = True
            self.oauth_state.start()
            oauth_started = True
            await self.human_ui_runtime.start()
            human_ui_started = True
        except BaseException:
            with suppress(BaseException):
                await self.stream_hub.aclose()
            if human_ui_started:
                with suppress(BaseException):
                    await self.human_ui_runtime.aclose()
            if oauth_started:
                with suppress(BaseException):
                    await self.oauth_state.aclose()
            if managed_jobs_started:
                with suppress(BaseException):
                    await self.managed_jobs_runtime.aclose()
            with suppress(BaseException):
                await self.session_copy_service.aclose()
            with suppress(BaseException):
                self.transfer_gateway.close()
            with suppress(BaseException):
                await self.executor_pairing.aclose()
            with suppress(BaseException):
                await self.session_coordinator.aclose()
            if executor_transport_started:
                with suppress(BaseException):
                    await self.executor_transport.aclose()
            with suppress(BaseException):
                self.control_state.close()
            self._closed = True
            raise

        self._started = True

    async def aclose(self) -> None:
        """Close every control-owned service; repeated close is harmless."""
        if self._close_complete:
            return
        self._closed = True
        errors: list[BaseException] = []

        async def close_async(close) -> None:
            try:
                await close()
            except BaseException as exc:
                errors.append(exc)

        await close_async(self.managed_jobs_runtime.aclose)
        await close_async(self.stream_hub.aclose)
        await close_async(self.human_ui_runtime.aclose)
        await close_async(self.oauth_state.aclose)
        await close_async(self.session_copy_service.aclose)
        try:
            self.transfer_gateway.close()
        except BaseException as exc:
            errors.append(exc)
        await close_async(self.executor_pairing.aclose)
        await close_async(self.session_coordinator.aclose)
        await close_async(self.executor_transport.aclose)
        try:
            self.control_state.close()
        except BaseException as exc:
            errors.append(exc)

        self._started = False
        if errors:
            raise errors[0]
        self._close_complete = True

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[ControlRuntime]:
        """Run the control ownership scope with deterministic cleanup."""
        try:
            await self.start()
            yield self
        finally:
            await self.aclose()


def build_control_runtime(settings: Settings) -> ControlRuntime:
    """Construct one control graph from one resolved role configuration."""
    config = resolve_control_config(settings)
    state_store = FileStateStore(lambda: config.state_dir)
    control_state = ControlState(state_store)
    executor_transport = ExecutorTransport(
        control_state,
        max_pending_commands=config.executor_max_pending_commands,
    )
    executor_pairing = ExecutorPairingService(
        control_state,
        executor_transport,
        verification_uri=config.resolved_base_url.rstrip("/") + "/pair",
        max_pending_attempts=config.executor_pairing_max_pending,
        ttl_s=config.executor_pairing_ttl_s,
    )
    task_service = ControlTaskService(control_state, state_store, config)
    session_coordinator = ControlSessionCoordinator(
        control_state,
        executor_transport,
        max_agent_sessions=config.max_agent_sessions,
        agent_session_retention_s=config.agent_session_retention_s,
        task_session_admitter=task_service.admit_session_attachment,
    )
    executor_fleet = ControlExecutorFleetService(
        control_state,
        executor_transport,
        session_coordinator,
        executor_pairing,
        config,
    )
    transfer_gateway = ControlTransferGateway(
        executor_transport, PayloadStore(config.data_dir)
    )
    object_store = S3ObjectTransferService(
        state_store,
        bucket=config.transfer_object_store_bucket,
        prefix=config.transfer_object_store_prefix,
        region=config.transfer_object_store_region,
        endpoint_url=config.transfer_object_store_endpoint_url,
        presign_ttl_s=config.transfer_object_store_presign_ttl_s,
    )
    session_copy_service = ControlSessionCopyService(
        session_coordinator,
        executor_transport,
        state_store,
        config.data_dir,
        transfer_gateway=transfer_gateway,
        object_store=object_store,
        max_transfer_payload_bytes=config.max_transfer_payload_bytes,
        max_transfer_payload_store_bytes=config.max_transfer_payload_store_bytes,
    )

    async def authenticated_proof(executor_id: str, credential: str) -> None:
        await executor_pairing.complete_authenticated_proof(
            executor_id, credential
        )

    async def authenticated_hello(executor_id: str, credential: str) -> None:
        _ = credential
        await session_coordinator.reconcile_hello(executor_id)
        session_copy_service.schedule_reconcile_abandonments(
            executor_id=executor_id
        )

    executor_transport.set_authenticated_proof_callback(authenticated_proof)
    executor_transport.set_authenticated_hello_callback(authenticated_hello)
    download_service = ControlDownloadService(
        session_coordinator, executor_transport, config
    )
    managed_jobs_runtime = ManagedJobsRuntime(state_store, config)
    job_service = ControlJobService(
        session_coordinator,
        managed_jobs_runtime,
        managed_retry_availability=session_copy_service.retry_require_available,
    )
    audit_service = ControlAuditService(task_service, control_state)
    session_coordinator.set_control_resource_hooks(
        auto_cleanup_blocked=job_service.auto_cleanup_blocked,
        before_terminate=job_service.stop_referencing_jobs,
    )
    managed_kind, managed_handler = (
        session_copy_service.managed_job_registration()
    )
    managed_jobs_runtime.register_handler(managed_kind, managed_handler)
    human_ui_runtime = build_human_ui_runtime()
    stream_hub = ControlStreamHub(
        max_streams=config.ui_terminal_max_connections,
        idle_timeout_s=config.ui_terminal_idle_timeout_s,
        reserve_slot=lambda: human_ui_runtime.terminal_connections.reserve(
            config.ui_terminal_max_connections
        ),
        release_slot=human_ui_runtime.terminal_connections.release,
    )
    executor_transport.set_executor_invalidated_callback(
        stream_hub.close_executor
    )
    oauth_state = build_oauth_state(config.state_dir, state_store=state_store)
    return ControlRuntime(
        config=config,
        state_store=state_store,
        control_state=control_state,
        executor_transport=executor_transport,
        executor_pairing=executor_pairing,
        executor_fleet=executor_fleet,
        session_coordinator=session_coordinator,
        session_copy_service=session_copy_service,
        transfer_gateway=transfer_gateway,
        download_service=download_service,
        job_service=job_service,
        task_service=task_service,
        audit_service=audit_service,
        managed_jobs_runtime=managed_jobs_runtime,
        stream_hub=stream_hub,
        human_ui_runtime=human_ui_runtime,
        oauth_state=oauth_state,
        tool_catalog=build_control_tool_catalog(
            config,
            session_coordinator,
            session_copy_service,
            job_service,
            download_service,
            executor_fleet,
            task_service,
            audit_service,
        ),
    )
