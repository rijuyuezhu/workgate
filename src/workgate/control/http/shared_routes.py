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
from .stream_routes import terminal_stream_routes


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
                *executor_bootstrap_routes(settings.resolved_base_url),
                *runtime.transfer_gateway.routes(),
                *terminal_stream_routes(
                    runtime.executor_transport, runtime.stream_hub
                ),
            ]
        )
    core_routes.extend(oauth_public_routes())

    ui_routes, ui_public_routes = human_ui_routes(settings)
    if runtime is not None:
        ui_routes.extend(config_inspection_routes(api_prefix=UI_API_PREFIX))
        ui_routes.extend(
            executor_admin_routes(
                runtime.executor_fleet,
                runtime.executor_pairing,
                api_prefix=UI_API_PREFIX,
            )
        )
    return core_routes, ui_routes, [*core_routes, *ui_public_routes]
