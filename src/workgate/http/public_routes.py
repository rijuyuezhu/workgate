"""Shared public HTTP route assembly for REST and MCP transports."""

from starlette.routing import Route

from .downloads import download_routes
from .health import health_routes


def public_http_routes() -> list[Route]:
    """Return public non-OAuth routes shared by REST and MCP HTTP apps."""
    return [*health_routes(), *download_routes()]
