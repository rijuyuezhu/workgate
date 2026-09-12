"""Shared health and readiness HTTP routes."""

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..version import version_info


def health_response(request: Request) -> JSONResponse:
    """Return a lightweight process health response."""
    return JSONResponse({"ok": True})


def version_response(request: Request) -> JSONResponse:
    """Return package and runtime version metadata."""
    return JSONResponse(version_info())


def ready_response(request: Request) -> JSONResponse:
    """Return process readiness without exposing executor workspace policy."""
    return JSONResponse({"ok": True})


def health_routes() -> list[Route]:
    """Return public Starlette health and readiness routes."""
    return [
        Route("/healthz", health_response, methods=["GET"]),
        Route("/readyz", ready_response, methods=["GET"]),
        Route("/version", version_response, methods=["GET"]),
    ]
