"""Read-only live configuration inspection; no writes or logs."""

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from ...config.inspection import current_startup_config
from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import SCOPE_EXECUTOR_USE, SCOPE_SHELL_READ


def _answer(data: Any, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        data,
        status_code=status_code,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def config_inspection_routes(*, api_prefix: str) -> list[BaseRoute]:
    async def inspect(request: Request) -> Response:
        executor_id = request.query_params.get("executor_id", "").strip()
        try:
            required = (
                (SCOPE_SHELL_READ, SCOPE_EXECUTOR_USE)
                if executor_id
                else (SCOPE_SHELL_READ,)
            )
            require_oauth_scopes(required)
        except MissingOAuthScopeError:
            return _answer({"ok": False, "error": "Forbidden"}, status_code=403)
        if not executor_id:
            snapshot = current_startup_config("control")
            if snapshot is None:
                return _answer(
                    {
                        "ok": False,
                        "error": "Unavailable",
                        "message": "Running Control configuration is unavailable",
                    },
                    status_code=503,
                )
            return _answer({"ok": True, "data": snapshot.describe()})
        if len(executor_id.encode("utf-8")) > 128:
            return _answer(
                {"ok": False, "error": "InvalidExecutorId"}, status_code=400
            )
        runtime = getattr(request.app.state, "control_runtime", None)
        if runtime is None:
            return _answer(
                {"ok": False, "error": "Unavailable"}, status_code=503
            )
        record = runtime.control_state.snapshot_executors().get(executor_id)
        if record is None or record.revoked_at is not None:
            return _answer(
                {"ok": False, "error": "ExecutorNotFound"}, status_code=404
            )
        if not await runtime.executor_transport.is_online(executor_id):
            return _answer(
                {
                    "ok": True,
                    "data": {
                        "role": "executor",
                        "executor_id": executor_id,
                        "status": "offline",
                        "fields": [],
                    },
                }
            )
        try:
            result = await runtime.executor_transport.call(
                executor_id,
                "ui.config.inspect",
                {},
                timeout_s=8,
            )
            if not result.ok or not isinstance(result.result, dict):
                raise RuntimeError("Remote inspection failed")
        except Exception:
            # Never echo a remote exception; it may include credential data.
            return _answer(
                {
                    "ok": False,
                    "error": "ExecutorUnavailable",
                    "message": "Executor configuration could not be read",
                },
                status_code=503,
            )
        return _answer(
            {
                "ok": True,
                "data": {
                    **result.result,
                    "executor_id": executor_id,
                },
            }
        )

    return [Route(api_prefix + "/config", inspect, methods=["GET"])]
