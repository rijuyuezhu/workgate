"""REST routes and middleware for local tool invocations."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from ...ops.shell import tool_timeout_s
from ...tools.catalog import ToolCatalog
from ...tools.contracts import ToolHandler
from .invocations import call_http_tool

type ToolRouteHandler = Callable[..., Awaitable[Any]]


def _disable_response_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"


def install_tool_cache_control_middleware(app: FastAPI) -> None:
    """Install no-store headers for GET REST tool responses."""

    @app.middleware("http")
    async def tool_cache_control_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        if request.method == "GET" and request.url.path.startswith("/tools/"):
            _disable_response_cache(response)
        return response


def install_tools_timeout_middleware(
    app: FastAPI, catalog: ToolCatalog
) -> None:
    """Install the tool timeout middleware for REST tool routes."""
    non_cancellable_routes = frozenset(
        (route.method, route.path)
        for route in catalog.http_routes()
        if not route.timeout_cancellable
    )

    @app.middleware("http")
    async def tools_timeout_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not request.url.path.startswith("/tools/"):
            return await call_next(request)
        if (request.method.upper(), request.url.path) in non_cancellable_routes:
            return await call_next(request)
        tool_name = request.url.path.removeprefix("/tools/").split("/", 1)[0]
        timeout_s = tool_timeout_s(tool_name)
        try:
            return await asyncio.wait_for(call_next(request), timeout=timeout_s)
        except TimeoutError:
            return JSONResponse(
                status_code=504,
                content={
                    "error": "tool_timeout",
                    "message": f"{request.url.path} exceeded {timeout_s} second tool timeout",
                },
            )


def register_http_tool_routes(app: FastAPI, catalog: ToolCatalog) -> None:
    """Register REST tool endpoints from the local tool routing table."""
    handlers = catalog.local_handlers()
    for route in catalog.http_routes():
        match route.method:
            case "GET":
                handler = handlers[route.tool_name]
                app.get(route.path)(
                    _make_get_tool_handler(route.tool_name, handler)
                )
            case "POST":
                handler = handlers[route.tool_name]
                app.post(route.path)(
                    _make_post_tool_handler(route.tool_name, handler)
                )
            case _:
                raise ValueError(
                    f"Unsupported HTTP tool method {route.method!r} for {route.path}"
                )


def _make_get_tool_handler(
    tool_name: str, handler: ToolHandler
) -> ToolRouteHandler:
    async def get_handler(request: Request) -> Any:
        args = dict(request.query_params)
        return await call_http_tool(tool_name, args or None, handler=handler)

    return get_handler


def _make_post_tool_handler(
    tool_name: str, handler: ToolHandler
) -> ToolRouteHandler:
    async def post_handler(body: dict[str, Any] | None = None) -> Any:
        return await call_http_tool(tool_name, body, handler=handler)

    return post_handler
