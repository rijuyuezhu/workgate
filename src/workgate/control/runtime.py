"""Control composition owner for long-lived server processes."""

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
from ..jobs.managed import (
    ManagedJobsRuntime,
    configure_managed_jobs_runtime,
)
from ..oauth.core.state import (
    OAuthState,
    build_oauth_state,
    configure_oauth_state,
)
from ..remote.manager import (
    RemoteManager,
    configure_remote_manager,
)
from ..terminal.runtime import TerminalRuntime, build_terminal_runtime
from ..tools.catalog import ToolCatalog
from ..ui.http.live_state import HumanUiRuntime, build_human_ui_runtime
from .search_composition import build_control_tool_catalog


@dataclass
class ControlRuntime:
    """Own the control's composed services and compatibility lifecycle."""

    settings: Settings
    """Resolved server settings for this control process."""
    services: RuntimeServices
    """Explicit shared state services owned by this runtime."""
    managed_jobs_runtime: ManagedJobsRuntime
    """Control-owned managed background-job tasks, handlers, and leases."""
    remote_manager: RemoteManager
    """Control-owned live remote-worker control-plane state."""
    terminal_runtime: TerminalRuntime
    """Control-owned terminal bridge and ConPTY live state."""
    human_ui_runtime: HumanUiRuntime
    """Control-owned Human UI terminal and remote-file live state."""
    oauth_state: OAuthState
    """Control-owned dynamic-client and authorization-code live state."""
    tool_catalog: ToolCatalog
    """Control tool catalog with the migrated Search service already bound."""
    _installation: RuntimeServiceInstallation | None = field(
        default=None, init=False, repr=False
    )
    _previous_managed_jobs_runtime: ManagedJobsRuntime | None = field(
        default=None, init=False, repr=False
    )
    _managed_jobs_binding_installed: bool = field(
        default=False, init=False, repr=False
    )
    _previous_remote_manager: RemoteManager | None = field(
        default=None, init=False, repr=False
    )
    _remote_binding_installed: bool = field(
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
        """Install compatibility bindings inside the owning async lifespan."""
        if self._closed:
            raise RuntimeError("ControlRuntime cannot be restarted after close")
        if self._installation is not None:
            return
        installation = install_runtime_services(self.services)
        managed_jobs_started = False
        managed_jobs_bound = False
        terminal_started = False
        remote_started = False
        remote_bound = False
        oauth_started = False
        oauth_bound = False
        human_ui_started = False
        previous_managed_jobs_runtime: ManagedJobsRuntime | None = None
        previous_remote_manager: RemoteManager | None = None
        previous_oauth_state: OAuthState | None = None
        try:
            await self.managed_jobs_runtime.start()
            managed_jobs_started = True
            previous_managed_jobs_runtime = configure_managed_jobs_runtime(
                self.managed_jobs_runtime
            )
            managed_jobs_bound = True
            await self.terminal_runtime.start()
            terminal_started = True
            await self.remote_manager.start()
            remote_started = True
            previous_remote_manager = configure_remote_manager(
                self.remote_manager
            )
            remote_bound = True
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
                            if remote_bound:
                                configure_remote_manager(
                                    previous_remote_manager
                                )
                        finally:
                            try:
                                if remote_started:
                                    await self.remote_manager.aclose()
                            finally:
                                try:
                                    if terminal_started:
                                        await self.terminal_runtime.aclose()
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
                                            installation.close()
                                            self._closed = True
            raise
        self._installation = installation
        self._previous_managed_jobs_runtime = previous_managed_jobs_runtime
        self._managed_jobs_binding_installed = True
        self._previous_remote_manager = previous_remote_manager
        self._remote_binding_installed = True
        self._previous_oauth_state = previous_oauth_state
        self._oauth_binding_installed = True

    async def aclose(self) -> None:
        """Restore prior compatibility bindings; repeated close is harmless."""
        installation = self._installation
        self._installation = None
        self._closed = True
        managed_jobs_error: BaseException | None = None
        human_ui_error: BaseException | None = None
        oauth_error: BaseException | None = None
        remote_error: BaseException | None = None
        terminal_error: BaseException | None = None
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
            try:
                await self.remote_manager.aclose()
            except BaseException as exc:
                remote_error = exc
            try:
                await self.terminal_runtime.aclose()
            except BaseException as exc:
                terminal_error = exc
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
            if self._remote_binding_installed:
                configure_remote_manager(self._previous_remote_manager)
                self._remote_binding_installed = False
                self._previous_remote_manager = None
            if installation is not None:
                installation.close()
        if managed_jobs_error is not None:
            raise managed_jobs_error
        if human_ui_error is not None:
            raise human_ui_error
        if oauth_error is not None:
            raise oauth_error
        if remote_error is not None:
            raise remote_error
        if terminal_error is not None:
            raise terminal_error

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
    services = build_runtime_services(settings)
    managed_jobs_runtime = ManagedJobsRuntime()
    from ..ops.utils.session_copy import session_copy_managed_job_registration

    managed_kind, managed_handler = session_copy_managed_job_registration()
    managed_jobs_runtime.register_handler(managed_kind, managed_handler)
    remote_manager = RemoteManager(
        lambda: settings,
        state_store=services.state_store,
    )
    terminal_runtime = build_terminal_runtime()
    human_ui_runtime = build_human_ui_runtime(remote_manager.call)
    oauth_state = build_oauth_state(settings.state_dir)
    return ControlRuntime(
        settings=settings,
        services=services,
        managed_jobs_runtime=managed_jobs_runtime,
        remote_manager=remote_manager,
        terminal_runtime=terminal_runtime,
        human_ui_runtime=human_ui_runtime,
        oauth_state=oauth_state,
        tool_catalog=build_control_tool_catalog(
            settings,
            services.tool_session_store,
            remote_manager,
        ),
    )
