"""Control composition owner for long-lived server processes."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from ..composition.services import (
    ControlServiceInstallation,
    ControlServices,
    build_control_services,
    install_control_services,
)
from ..config.settings import Settings
from ..jobs.managed import (
    ManagedJobsRuntime,
    configure_managed_jobs_runtime,
)
from ..oauth.core.state import (
    OAuthState,
    build_oauth_state,
    configure_oauth_state,
)
from ..tools.catalog import ToolCatalog
from ..ui.http.live_state import HumanUiRuntime, build_human_ui_runtime
from .audit import ControlAuditService
from .config import ControlConfig, resolve_control_config
from .downloads import ControlDownloadService
from .executor_transport import ExecutorTransport
from .jobs import ControlJobService
from .pairing import ExecutorPairingService
from .search_composition import build_control_tool_catalog
from .session_copy import ControlSessionCopyService
from .sessions import ControlSessionCoordinator
from .state import ControlState
from .todos import ControlTodoService


@dataclass
class ControlRuntime:
    """Own the control's composed services and compatibility lifecycle."""

    config: ControlConfig
    """Resolved control-owned authority for new composition code."""
    services: ControlServices
    """Explicit shared state services owned by this runtime."""
    control_state: ControlState
    """Restart-critical durable control facts backed by the shared state store."""
    executor_transport: ExecutorTransport
    """Process-local ordinary RPC queues, presence, polls, and result waiters."""
    executor_pairing: ExecutorPairingService
    """Process-local device-code pairing attempts and transient credential delivery."""
    session_coordinator: ControlSessionCoordinator
    """Control authority for final shared session lifecycle and executor routing."""
    session_copy_service: ControlSessionCopyService
    """Control orchestration for copies between existing shared sessions."""
    download_service: ControlDownloadService
    """Control-owned public file-link snapshots sourced through executor RPC."""
    job_service: ControlJobService
    """Hybrid public job routing across executor resources and control-managed jobs."""
    todo_service: ControlTodoService
    """Control-owned revisioned todo state for shared sessions."""
    audit_service: ControlAuditService
    """Control authority for canonical public audit history."""
    managed_jobs_runtime: ManagedJobsRuntime
    """Control-owned managed background-job tasks, handlers, and leases."""
    human_ui_runtime: HumanUiRuntime
    """Control-owned Human UI connection admission state."""
    oauth_state: OAuthState
    """Control-owned dynamic-client and authorization-code live state."""
    tool_catalog: ToolCatalog
    """Control tool catalog with the migrated Search service already bound."""
    _installation: ControlServiceInstallation | None = field(
        default=None, init=False, repr=False
    )
    _previous_managed_jobs_runtime: ManagedJobsRuntime | None = field(
        default=None, init=False, repr=False
    )
    _managed_jobs_binding_installed: bool = field(
        default=False, init=False, repr=False
    )
    _previous_oauth_state: OAuthState | None = field(
        default=None, init=False, repr=False
    )
    _oauth_binding_installed: bool = field(
        default=False, init=False, repr=False
    )
    _closed: bool = field(default=False, init=False, repr=False)

    async def start(self) -> None:
        """Install control-owned compatibility bindings inside the async lifespan."""
        if self._closed:
            raise RuntimeError("ControlRuntime cannot be restarted after close")
        if self._installation is not None:
            return
        installation = install_control_services(self.services)
        managed_jobs_started = False
        managed_jobs_bound = False
        oauth_started = False
        oauth_bound = False
        human_ui_started = False
        executor_transport_started = False
        previous_managed_jobs_runtime: ManagedJobsRuntime | None = None
        previous_oauth_state: OAuthState | None = None
        try:
            self.control_state.start()
            self.executor_transport.start()
            executor_transport_started = True
            await self.managed_jobs_runtime.start()
            managed_jobs_started = True
            previous_managed_jobs_runtime = configure_managed_jobs_runtime(
                self.managed_jobs_runtime
            )
            managed_jobs_bound = True
            self.oauth_state.start()
            oauth_started = True
            previous_oauth_state = configure_oauth_state(self.oauth_state)
            oauth_bound = True
            await self.human_ui_runtime.start()
            human_ui_started = True
        except BaseException:
            try:
                if human_ui_started:
                    await self.human_ui_runtime.aclose()
            finally:
                try:
                    if oauth_bound:
                        configure_oauth_state(previous_oauth_state)
                finally:
                    try:
                        if oauth_started:
                            await self.oauth_state.aclose()
                    finally:
                        try:
                            if managed_jobs_bound:
                                configure_managed_jobs_runtime(
                                    previous_managed_jobs_runtime
                                )
                        finally:
                            try:
                                if managed_jobs_started:
                                    await self.managed_jobs_runtime.aclose()
                            finally:
                                try:
                                    await self.executor_pairing.aclose()
                                finally:
                                    try:
                                        await self.session_coordinator.aclose()
                                    finally:
                                        try:
                                            if executor_transport_started:
                                                await self.executor_transport.aclose()
                                        finally:
                                            try:
                                                self.control_state.close()
                                            finally:
                                                installation.close()
                                                self._closed = True
            raise
        self._installation = installation
        self._previous_managed_jobs_runtime = previous_managed_jobs_runtime
        self._managed_jobs_binding_installed = True
        self._previous_oauth_state = previous_oauth_state
        self._oauth_binding_installed = True

    async def aclose(self) -> None:
        """Restore prior control bindings; repeated close is harmless."""
        installation = self._installation
        self._installation = None
        self._closed = True
        managed_jobs_error: BaseException | None = None
        human_ui_error: BaseException | None = None
        oauth_error: BaseException | None = None
        try:
            try:
                await self.managed_jobs_runtime.aclose()
            except BaseException as exc:
                managed_jobs_error = exc
            try:
                await self.human_ui_runtime.aclose()
            except BaseException as exc:
                human_ui_error = exc
            try:
                await self.oauth_state.aclose()
            except BaseException as exc:
                oauth_error = exc
        finally:
            if self._managed_jobs_binding_installed:
                configure_managed_jobs_runtime(
                    self._previous_managed_jobs_runtime
                )
                self._managed_jobs_binding_installed = False
                self._previous_managed_jobs_runtime = None
            if self._oauth_binding_installed:
                configure_oauth_state(self._previous_oauth_state)
                self._oauth_binding_installed = False
                self._previous_oauth_state = None
            try:
                await self.executor_pairing.aclose()
            finally:
                try:
                    await self.session_coordinator.aclose()
                finally:
                    try:
                        await self.executor_transport.aclose()
                    finally:
                        try:
                            self.control_state.close()
                        finally:
                            if installation is not None:
                                installation.close()
        if managed_jobs_error is not None:
            raise managed_jobs_error
        if human_ui_error is not None:
            raise human_ui_error
        if oauth_error is not None:
            raise oauth_error

    @asynccontextmanager
    async def lifespan(self) -> AsyncGenerator[ControlRuntime]:
        """Run the control ownership scope with deterministic cleanup."""
        try:
            await self.start()
            yield self
        finally:
            await self.aclose()


def build_control_runtime(settings: Settings) -> ControlRuntime:
    """Construct one control graph without installing process globals yet."""
    services = build_control_services(settings)
    config = resolve_control_config(settings)
    control_state = ControlState(services.state_store)
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
    session_coordinator = ControlSessionCoordinator(
        control_state,
        executor_transport,
        max_agent_sessions=config.max_agent_sessions,
        agent_session_retention_s=config.agent_session_retention_s,
    )

    async def authenticated_hello(executor_id: str, credential: str) -> None:
        await executor_pairing.complete_authenticated_hello(
            executor_id, credential
        )
        await session_coordinator.reconcile_hello(executor_id)

    executor_transport.set_authenticated_hello_callback(authenticated_hello)
    session_copy_service = ControlSessionCopyService(
        session_coordinator, executor_transport
    )
    download_service = ControlDownloadService(
        session_coordinator, executor_transport, config
    )
    job_service = ControlJobService(session_coordinator)
    todo_service = ControlTodoService(
        control_state, services.state_store, config
    )
    audit_service = ControlAuditService(session_coordinator)
    session_coordinator.set_control_resource_hooks(
        auto_cleanup_blocked=job_service.auto_cleanup_blocked,
        before_terminate=job_service.stop_referencing_jobs,
    )
    managed_jobs_runtime = ManagedJobsRuntime(services.state_store)
    managed_kind, managed_handler = (
        session_copy_service.managed_job_registration()
    )
    managed_jobs_runtime.register_handler(managed_kind, managed_handler)
    human_ui_runtime = build_human_ui_runtime()
    oauth_state = build_oauth_state(
        config.state_dir, state_store=services.state_store
    )
    return ControlRuntime(
        config=config,
        services=services,
        control_state=control_state,
        executor_transport=executor_transport,
        executor_pairing=executor_pairing,
        session_coordinator=session_coordinator,
        session_copy_service=session_copy_service,
        download_service=download_service,
        job_service=job_service,
        todo_service=todo_service,
        audit_service=audit_service,
        managed_jobs_runtime=managed_jobs_runtime,
        human_ui_runtime=human_ui_runtime,
        oauth_state=oauth_state,
        tool_catalog=build_control_tool_catalog(
            config,
            session_coordinator,
            session_copy_service,
            job_service,
            download_service,
            todo_service,
            audit_service,
        ),
    )
