"""HTTP routes for remote worker bootstrap, polling, and results."""

import json

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .bundle import worker_bundle
from .constants import (
    REMOTE_API_PREFIX,
    REMOTE_WORKER_BUNDLE_PATH,
)
from .manager import WorkerRuntimeCompatibilityError, remote_manager
from .responses import _error, _ok


def _bearer_token(request: Request) -> str:
    """Extract the worker bearer token from an Authorization header."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return ""


async def _optional_json_object(request: Request) -> dict[str, object]:
    """Decode an optional JSON object without breaking legacy empty polls."""
    body = await request.body()
    if not body.strip():
        return {}
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request body must be valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("request body must be a JSON object")
    return decoded


async def register_endpoint(request: Request) -> JSONResponse:
    """Register a worker over HTTP and return its long-poll token."""
    try:
        return JSONResponse(
            _ok(await remote_manager().register_worker(await request.json()))
        )
    except WorkerRuntimeCompatibilityError as exc:
        return _error(str(exc), type(exc).__name__, 409)
    except Exception as exc:
        return _error(str(exc), type(exc).__name__, 400)


async def resume_endpoint(request: Request) -> JSONResponse:
    """Resume a previously registered worker using its persisted long-poll token."""
    try:
        return JSONResponse(
            _ok(
                await remote_manager().resume_worker(
                    _bearer_token(request), await request.json()
                )
            )
        )
    except WorkerRuntimeCompatibilityError as exc:
        return _error(str(exc), type(exc).__name__, 409)
    except Exception as exc:
        return _error(str(exc), type(exc).__name__, 401)


async def poll_endpoint(request: Request) -> JSONResponse:
    """Negotiate worker runtime state, then long-poll for its next job."""
    try:
        payload = await _optional_json_object(request)
        return JSONResponse(
            _ok(await remote_manager().poll(_bearer_token(request), payload))
        )
    except PermissionError as exc:
        return _error(str(exc), type(exc).__name__, 401)
    except WorkerRuntimeCompatibilityError as exc:
        return _error(str(exc), type(exc).__name__, 409)
    except ValueError as exc:
        return _error(str(exc), type(exc).__name__, 400)
    except Exception as exc:
        return _error(str(exc), type(exc).__name__, 500)


async def heartbeat_endpoint(request: Request) -> JSONResponse:
    """Refresh worker liveness while a long-running job is executing."""
    try:
        return JSONResponse(
            _ok(await remote_manager().heartbeat(_bearer_token(request)))
        )
    except Exception as exc:
        return _error(str(exc), type(exc).__name__, 401)


async def result_endpoint(request: Request) -> JSONResponse:
    """Accept the authenticated worker's result for its current job."""
    try:
        return JSONResponse(
            _ok(
                await remote_manager().submit_result(
                    _bearer_token(request), await request.json()
                )
            )
        )
    except Exception as exc:
        return _error(str(exc), type(exc).__name__, 401)


def remote_routes() -> list[Route]:
    """Create the APIRouter containing worker bootstrap and control endpoints."""
    return [
        Route(REMOTE_WORKER_BUNDLE_PATH, worker_bundle, methods=["GET"]),
        Route(
            f"{REMOTE_API_PREFIX}/register", register_endpoint, methods=["POST"]
        ),
        Route(f"{REMOTE_API_PREFIX}/resume", resume_endpoint, methods=["POST"]),
        Route(f"{REMOTE_API_PREFIX}/poll", poll_endpoint, methods=["POST"]),
        Route(
            f"{REMOTE_API_PREFIX}/heartbeat",
            heartbeat_endpoint,
            methods=["POST"],
        ),
        Route(f"{REMOTE_API_PREFIX}/result", result_endpoint, methods=["POST"]),
    ]
