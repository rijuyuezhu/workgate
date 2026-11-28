"""Non-tool routes shared by Control's MCP and debug HTTP hosts."""

from starlette.routing import BaseRoute

from ...config.control import ControlConfig
from ...http.public_routes import public_http_routes
from ...oauth.http.routes import oauth_public_routes
from ...ui.http.routes import UI_API_PREFIX, human_ui_routes
from ..executor_bootstrap import executor_bootstrap_routes
from ..runtime import ControlRuntime
from .config_inspection import config_inspection_routes
from .executor_admin import executor_admin_routes
from .executor_routes import executor_routes
from .hosted_chat import hosted_chat_generation_routes
from .hosted_chat_agents import hosted_chat_agent_routes
from .hosted_chat_workspace import hosted_chat_workspace_routes
from .integration_oauth import browser_mcp_oauth_routes
from .stream_routes import desktop_stream_routes, terminal_stream_routes


def control_http_routes(
    settings: ControlConfig,
    runtime: ControlRuntime | None,
) -> tuple[list[BaseRoute], list[BaseRoute], list[BaseRoute]]:
    """Return core, UI/admin and OAuth-exempt routes in stable order."""
    core_routes: list[BaseRoute] = [*public_http_routes(settings)]
    if runtime is not None:
        core_routes.extend(
            [
                *executor_routes(
                    runtime.executor_transport, runtime.executor_pairing
                ),
                *executor_bootstrap_routes(),
                *runtime.transfer_gateway.routes(),
                *terminal_stream_routes(
                    runtime.executor_transport, runtime.stream_hub
                ),
                *desktop_stream_routes(
                    runtime.executor_transport, runtime.desktop_stream_hub
                ),
            ]
        )
    core_routes.extend(oauth_public_routes())

    ui_routes, ui_public_routes = human_ui_routes(settings)
    if runtime is not None:
        if settings.ui_enabled and settings.hosted_chat_enabled:
            ui_routes.extend(hosted_chat_generation_routes(runtime))
            ui_routes.extend(hosted_chat_workspace_routes(runtime))
            ui_routes.extend(hosted_chat_agent_routes(runtime))
        ui_routes.extend(config_inspection_routes(api_prefix=UI_API_PREFIX))
        ui_routes.extend(
            executor_admin_routes(
                runtime.executor_fleet,
                runtime.executor_pairing,
                api_prefix=UI_API_PREFIX,
            )
        )
    if runtime is not None:
        oauth_protected, oauth_public = browser_mcp_oauth_routes()
        ui_routes.extend([*oauth_protected, *oauth_public])
        ui_public_routes.extend(oauth_public)
    return core_routes, ui_routes, [*core_routes, *ui_public_routes]
