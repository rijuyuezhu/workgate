"""Fork-native Human UI routes shared by browser and future OpenTUI clients."""

import hashlib
import html
import json
import time
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
)
from starlette.routing import BaseRoute, Route, WebSocketRoute

from ...config.settings import Settings, get_settings
from ...oauth.core.scopes import default_scope
from ...oauth.core.urls import issuer_url, resource_url
from ...version import version_info
from ..runtime import tui_runtime_available
from ..security import UI_API_PREFIX
from ..session import (
    UI_CSRF_HEADER,
    UI_SESSION_BINDING_HEADER,
    UI_SESSION_BINDING_PROTOCOL_PREFIX,
    UI_SESSION_BINDING_STORAGE_KEY,
    UI_SESSION_ESTABLISHED_STORAGE_KEY,
    ui_csrf_cookie_name,
)
from .audit import api_audit, api_audit_detail
from .dashboard import api_dashboard
from .files import (
    api_file_action,
    api_file_content,
    api_file_preview,
    api_files,
)
from .opentui import ui_opentui_websocket
from .session import (
    api_ui_session_logout,
    api_ui_session_oauth,
    api_ui_session_token,
    ui_request_origin,
)
from .session_snapshot import api_session_snapshot
from .sessions import api_session_action, api_sessions
from .terminals import (
    api_terminal_action,
    api_terminal_read,
    api_terminals,
    ui_terminal_websocket,
)
from .todos import api_todos


def _json_ok(data: Any = None, message: str = "") -> JSONResponse:
    """Return the stable Human UI success envelope."""
    return JSONResponse({"ok": True, "message": message, "data": data})


def _assets_dir() -> Path:
    """Return the packaged browser asset directory."""
    return Path(__file__).resolve().parents[1] / "static"


@lru_cache(maxsize=1)
def _ui_asset_revision() -> str:
    """Return a content revision used to keep the HTML and assets in sync."""
    digest = hashlib.sha256()
    for name in (
        "xterm.css",
        "web.css",
        "xterm_bundle.js",
        "terminal_renderer.js",
        "syntax_highlight.js",
        "web.js",
        "dashboard.js",
        "executors.js",
        "audit_view.js",
        "audit.js",
        "sessions.js",
        "terminal.js",
        "files.js",
        "opentui_console.js",
    ):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((_assets_dir() / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _ui_index_html(settings: Settings, origin: str) -> str:
    """Load the browser shell and inject non-sensitive runtime configuration."""
    path = _assets_dir() / "index.html"
    if not path.is_file():
        return (
            '<!doctype html><html><head><meta charset="utf-8">'
            "<title>Workgate Human UI</title></head>"
            "<body><h1>Human UI assets are not installed</h1></body></html>"
        )
    oauth = None
    if settings.auth_mode == "oauth":
        oauth = {
            "issuer": issuer_url(),
            "resource": resource_url(),
            "scope": default_scope(),
            "registrationEndpoint": "/oauth/register",
            "authorizationEndpoint": "/oauth/authorize",
            "tokenEndpoint": "/oauth/token",
            "sessionOAuthEndpoint": UI_API_PREFIX + "/session/oauth",
            "sessionTokenEndpoint": UI_API_PREFIX + "/session/token",
            "sessionLogoutEndpoint": UI_API_PREFIX + "/session/logout",
        }
    asset_revision = _ui_asset_revision()
    config = html.escape(
        json.dumps(
            {
                "uiPath": settings.ui_path,
                "assetRevision": asset_revision,
                "apiPrefix": UI_API_PREFIX,
                "authMode": settings.auth_mode,
                "wallpaper": settings.ui_wallpaper,
                "opentuiAvailable": tui_runtime_available(settings),
                "csrfCookieName": ui_csrf_cookie_name(origin),
                "csrfHeaderName": UI_CSRF_HEADER,
                "sessionBindingHeaderName": UI_SESSION_BINDING_HEADER,
                "sessionBindingProtocolPrefix": UI_SESSION_BINDING_PROTOCOL_PREFIX,
                "sessionBindingStorageKey": UI_SESSION_BINDING_STORAGE_KEY,
                "sessionEstablishedStorageKey": UI_SESSION_ESTABLISHED_STORAGE_KEY,
                "oauth": oauth,
            },
            separators=(",", ":"),
        ),
        quote=True,
    )
    return (
        path.read_text(encoding="utf-8")
        .replace("__WORKGATE_UI_PATH__", settings.ui_path)
        .replace("__WORKGATE_UI_ASSET_REV__", asset_revision)
        .replace("__WORKGATE_UI_CONFIG_JSON__", config)
    )


def _index_headers() -> dict[str, str]:
    """Return restrictive headers for the public browser shell."""
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


async def ui_index(request: Request) -> Response:
    """Serve the public browser shell; API calls remain authenticated."""
    settings = get_settings()
    try:
        origin = ui_request_origin(request)
    except UnicodeError, ValueError:
        return Response("Invalid Human UI request origin", status_code=400)
    return HTMLResponse(
        _ui_index_html(settings, origin),
        headers=_index_headers(),
    )


async def ui_asset(request: Request) -> Response:
    """Serve one packaged browser asset without allowing path traversal."""
    raw = request.path_params.get("path", "")
    relative = PurePosixPath(str(raw))
    if relative.is_absolute() or ".." in relative.parts:
        return Response("Not found", status_code=404)

    assets = _assets_dir().resolve()
    path = assets.joinpath(*relative.parts)
    try:
        path.resolve().relative_to(assets)
    except OSError, ValueError:
        return Response("Not found", status_code=404)
    if not path.is_file():
        return Response("Not found", status_code=404)
    return FileResponse(
        path,
        headers={
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _control_runtime(request: Request) -> Any:
    runtime = getattr(request.app.state, "control_runtime", None)
    if runtime is None:
        raise RuntimeError("Human UI requires the control runtime")
    return runtime


async def _executor_targets(request: Request) -> dict[str, Any]:
    """Return trusted executor targets used by Human UI machine-facing views."""
    runtime = _control_runtime(request)
    records = sorted(
        (
            record
            for record in runtime.control_state.snapshot_executors().values()
            if record.revoked_at is None
        ),
        key=lambda record: (record.name.casefold(), record.executor_id),
    )
    now = time.time()
    rows: list[dict[str, Any]] = []
    for record in records:
        inventory = await runtime.executor_transport.inventory(
            record.executor_id
        )
        online = await runtime.executor_transport.is_online(record.executor_id)
        last_seen = await runtime.executor_transport.last_seen_at(
            record.executor_id
        )
        rows.append(
            {
                "executor_id": record.executor_id,
                "name": record.name,
                "status": "online" if online else "offline",
                "workspace_root": (
                    "" if inventory is None else inventory.workspace_root
                ),
                "last_seen_at": last_seen,
                "last_seen_age_s": (
                    None if last_seen is None else max(0.0, now - last_seen)
                ),
                "queue_depth": await runtime.executor_transport.pending_count(
                    record.executor_id
                ),
                "capabilities": (
                    [] if inventory is None else list(inventory.capabilities)
                ),
                "runtime": (
                    None
                    if inventory is None
                    else inventory.runtime.model_dump(mode="json")
                ),
            }
        )
    online_count = sum(row["status"] == "online" for row in rows)
    return {
        "executor_targets": rows,
        "executor_counts": {
            "online": online_count,
            "offline": len(rows) - online_count,
            "total": len(rows),
        },
    }


async def api_bootstrap(request: Request) -> Response:
    """Return initial authenticated state for browser and native UI clients."""
    settings = get_settings()
    return _json_ok(
        {
            "version": version_info(),
            "ui": {
                "path": settings.ui_path,
                "api_prefix": UI_API_PREFIX,
                "auth_mode": settings.auth_mode,
                "features": {
                    "dashboard": True,
                    "executors": True,
                    "terminals": True,
                    "terminal_websocket": True,
                    "files": True,
                    "file_preview": True,
                    "syntax_highlighting": True,
                    "audit_image_preview": True,
                    "wallpaper": settings.ui_wallpaper,
                    "opentui": tui_runtime_available(settings),
                    "file_editor": True,
                    "file_copy": True,
                    "file_move": True,
                    "file_rename": True,
                    "sessions": True,
                    "todos": True,
                    "audit": True,
                },
            },
            **await _executor_targets(request),
        }
    )


def human_ui_routes(
    settings: Settings,
) -> tuple[list[BaseRoute], list[BaseRoute]]:
    """Return all Human UI routes and the subset that must remain public."""
    if not settings.ui_enabled:
        return [], []
    ui_path = settings.ui_path
    public_routes: list[BaseRoute] = [
        Route("/pair", ui_index, methods=["GET"]),
        Route(ui_path, ui_index, methods=["GET"]),
        Route(ui_path + "/", ui_index, methods=["GET"]),
        Route(ui_path + "/callback", ui_index, methods=["GET"]),
        Route(ui_path + "/assets/{path:path}", ui_asset, methods=["GET"]),
        Route(
            UI_API_PREFIX + "/session/oauth",
            api_ui_session_oauth,
            methods=["POST"],
        ),
        Route(
            UI_API_PREFIX + "/session/token",
            api_ui_session_token,
            methods=["POST"],
        ),
        Route(
            UI_API_PREFIX + "/session/logout",
            api_ui_session_logout,
            methods=["POST"],
        ),
    ]
    protected_routes: list[BaseRoute] = [
        WebSocketRoute(
            ui_path + "/ws/terminals/{shell_id}", ui_terminal_websocket
        ),
        WebSocketRoute(ui_path + "/ws/opentui", ui_opentui_websocket),
        Route(UI_API_PREFIX + "/bootstrap", api_bootstrap, methods=["GET"]),
        Route(UI_API_PREFIX + "/dashboard", api_dashboard, methods=["GET"]),
        Route(UI_API_PREFIX + "/sessions", api_sessions, methods=["GET"]),
        Route(
            UI_API_PREFIX + "/sessions/snapshot",
            api_session_snapshot,
            methods=["GET"],
        ),
        Route(
            UI_API_PREFIX + "/sessions/{action}",
            api_session_action,
            methods=["POST"],
        ),
        Route(UI_API_PREFIX + "/files", api_files, methods=["GET"]),
        Route(
            UI_API_PREFIX + "/files/preview",
            api_file_preview,
            methods=["GET"],
        ),
        Route(
            UI_API_PREFIX + "/files/content",
            api_file_content,
            methods=["GET"],
        ),
        Route(
            UI_API_PREFIX + "/files/{action}",
            api_file_action,
            methods=["POST"],
        ),
        Route(UI_API_PREFIX + "/todos", api_todos, methods=["GET", "PUT"]),
        Route(UI_API_PREFIX + "/audit", api_audit, methods=["GET"]),
        Route(
            UI_API_PREFIX + "/audit/detail",
            api_audit_detail,
            methods=["GET"],
        ),
        Route(UI_API_PREFIX + "/terminals", api_terminals, methods=["GET"]),
        Route(
            UI_API_PREFIX + "/terminals/read",
            api_terminal_read,
            methods=["GET"],
        ),
        Route(
            UI_API_PREFIX + "/terminals/{action}",
            api_terminal_action,
            methods=["POST"],
        ),
    ]
    return [*public_routes, *protected_routes], public_routes
