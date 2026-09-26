"""Shared public HTTP route assembly for REST and MCP transports."""

from starlette.routing import Route

from ..config.control import ControlConfig
from .downloads import download_routes
from .health import health_routes


def public_http_routes(
    settings: ControlConfig | None = None,
) -> list[Route]:
    """Return public non-OAuth routes shared by REST and MCP HTTP apps."""
    return [*health_routes(), *download_routes(settings)]
