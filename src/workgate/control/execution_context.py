"""Execution-scoped bindings for one explicit control runtime."""

from collections.abc import Generator
from contextlib import ExitStack, contextmanager

from starlette.types import ASGIApp, Receive, Scope, Send

from ..config.control import ControlConfig
from ..config.role_config import use_role_config
from ..jobs.managed import ManagedJobsRuntime, use_managed_jobs_runtime
from ..oauth.core.state import OAuthState, use_oauth_state
from ..persistence import StateStore, use_state_store


@contextmanager
def control_execution_context(
    *,
    config: ControlConfig,
    state_store: StateStore,
    oauth_state: OAuthState | None = None,
    managed_jobs_runtime: ManagedJobsRuntime | None = None,
) -> Generator[None]:
    """Bind control-owned execution dependencies to the current task/thread."""
    with ExitStack() as stack:
        stack.enter_context(use_role_config(config))
        stack.enter_context(use_state_store(state_store))
        if oauth_state is not None:
            stack.enter_context(use_oauth_state(oauth_state))
        if managed_jobs_runtime is not None:
            stack.enter_context(use_managed_jobs_runtime(managed_jobs_runtime))
        yield


class ControlExecutionContextMiddleware:
    """Bind one control runtime's scoped dependencies for every ASGI scope."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        config: ControlConfig,
        state_store: StateStore,
        oauth_state: OAuthState | None = None,
        managed_jobs_runtime: ManagedJobsRuntime | None = None,
    ) -> None:
        self.app = app
        self.config = config
        self.state_store = state_store
        self.oauth_state = oauth_state
        self.managed_jobs_runtime = managed_jobs_runtime

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        with control_execution_context(
            config=self.config,
            state_store=self.state_store,
            oauth_state=self.oauth_state,
            managed_jobs_runtime=self.managed_jobs_runtime,
        ):
            await self.app(scope, receive, send)
