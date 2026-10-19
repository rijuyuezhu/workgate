"""Read-only live configuration inspection; no writes or logs."""

import asyncio
import json
from typing import Any

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from ...config.editing import ConfigEditError, save_config, validate_apply
from ...config.inspection import current_startup_config
from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import (
    SCOPE_EXECUTOR_USE,
    SCOPE_SHELL_READ,
    SCOPE_SHELL_WRITE,
)
from ...supervisor import request_restart, restart_status


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
            return _answer(
                {
                    "ok": True,
                    "data": {
                        **snapshot.describe(),
                        "apply_status": restart_status(),
                    },
                }
            )
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

    async def save(request: Request) -> Response:
        executor_id = request.query_params.get("executor_id", "").strip()
        try:
            scopes = (
                (SCOPE_SHELL_WRITE, SCOPE_EXECUTOR_USE)
                if executor_id
                else (SCOPE_SHELL_WRITE,)
            )
            require_oauth_scopes(scopes)
        except MissingOAuthScopeError:
            return _answer({"ok": False, "error": "Forbidden"}, status_code=403)
        if len(executor_id.encode()) > 128:
            return _answer(
                {"ok": False, "error": "InvalidExecutorId"}, status_code=400
            )
        try:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 65536:
                    raise ConfigEditError("InvalidEdit")
            payload = json.loads(body)
            if not isinstance(payload, dict) or set(payload) != {
                "revision",
                "changes",
            }:
                raise ConfigEditError("InvalidEdit")
            revision, changes = payload["revision"], payload["changes"]
            if not executor_id:
                snapshot = current_startup_config("control")
                if snapshot is None:
                    raise ConfigEditError("Unavailable", 503)
                result = await asyncio.to_thread(
                    save_config, snapshot, revision=revision, changes=changes
                )
                return _answer({"ok": True, "data": {"revision": result}})
            runtime = getattr(request.app.state, "control_runtime", None)
            if runtime is None:
                raise ConfigEditError("Unavailable", 503)
            record = runtime.control_state.snapshot_executors().get(executor_id)
            if record is None or record.revoked_at is not None:
                raise ConfigEditError("ExecutorNotFound", 404)
            if not await runtime.executor_transport.is_online(executor_id):
                raise ConfigEditError("ExecutorOffline", 409)
            reply = await runtime.executor_transport.call(
                executor_id,
                "ui.config.save",
                {"revision": revision, "changes": changes},
                timeout_s=12,
            )
            if not reply.ok or not isinstance(reply.result, dict):
                raise ConfigEditError("ConfigWriteFailed", 503)
            if "error" in reply.result:
                code = reply.result["error"]
                if code in {
                    "RevisionConflict",
                    "InvalidConfigValue",
                    "InvalidEdit",
                    "ConfigNotWritable",
                    "UnsafeConfigPath",
                    "ConfigNotOwned",
                    "UnsafeConfigPermissions",
                    "NoConfigFile",
                    "InvalidConfigFile",
                    "ConfigWriteFailed",
                }:
                    raise ConfigEditError(
                        code,
                        409
                        if code
                        in {
                            "RevisionConflict",
                            "InvalidConfigFile",
                            "NoConfigFile",
                        }
                        else 400,
                    )
                raise ConfigEditError("ConfigWriteFailed", 503)
            return _answer(
                {"ok": True, "data": {"revision": reply.result["revision"]}}
            )
        except ValueError, TypeError, json.JSONDecodeError:
            return _answer(
                {"ok": False, "error": "InvalidEdit"}, status_code=400
            )
        except ConfigEditError as exc:
            return _answer(
                {"ok": False, "error": exc.code}, status_code=exc.status
            )
        except Exception:
            # No traceback/arguments/secret values on the wire.
            return _answer(
                {"ok": False, "error": "ConfigWriteFailed"}, status_code=503
            )

    async def apply(request: Request) -> Response:
        executor_id = request.query_params.get("executor_id", "").strip()
        try:
            require_oauth_scopes(
                (SCOPE_SHELL_WRITE, SCOPE_EXECUTOR_USE)
                if executor_id
                else (SCOPE_SHELL_WRITE,)
            )
        except MissingOAuthScopeError:
            return _answer({"ok": False, "error": "Forbidden"}, status_code=403)
        if len(executor_id.encode()) > 128:
            return _answer(
                {"ok": False, "error": "InvalidExecutorId"}, status_code=400
            )
        try:
            if len(await request.body()) > 1024:
                raise ConfigEditError("InvalidEdit")
            payload = json.loads(await request.body())
            if not isinstance(payload, dict) or set(payload) != {"revision"}:
                raise ConfigEditError("InvalidEdit")
            revision = payload["revision"]
            if not isinstance(revision, str) or len(revision) != 64:
                raise ConfigEditError("InvalidEdit")
            if not executor_id:
                snapshot = current_startup_config("control")
                if snapshot is None:
                    raise ConfigEditError("Unavailable", 503)
                state = restart_status()
                if state == "shared":
                    raise ConfigEditError("SharedConfigFile", 409)
                if state not in {"healthy", "recovered"}:
                    raise ConfigEditError("ParentUnavailable", 409)
                await asyncio.to_thread(
                    validate_apply, snapshot, revision=revision
                )
                if not snapshot.describe()["file_changed_since_startup"]:
                    raise ConfigEditError("NothingToApply", 409)
                # BackgroundTask runs only after the HTTP 202 response is sent.
                return JSONResponse(
                    {"ok": True, "data": {"status": "accepted"}},
                    status_code=202,
                    headers={"Cache-Control": "no-store"},
                    background=BackgroundTask(request_restart, revision),
                )
            runtime = getattr(request.app.state, "control_runtime", None)
            if runtime is None:
                raise ConfigEditError("Unavailable", 503)
            record = runtime.control_state.snapshot_executors().get(executor_id)
            if record is None or record.revoked_at is not None:
                raise ConfigEditError("ExecutorNotFound", 404)
            if not await runtime.executor_transport.is_online(executor_id):
                raise ConfigEditError("ExecutorOffline", 409)
            reply = await runtime.executor_transport.call(
                executor_id,
                "ui.config.apply",
                {"revision": revision},
                timeout_s=12,
            )
            if not reply.ok or not isinstance(reply.result, dict):
                raise ConfigEditError("ExecutorUnavailable", 503)
            if "error" in reply.result:
                code = reply.result["error"]
                if code in {
                    "RevisionConflict",
                    "InvalidConfigValue",
                    "InvalidConfigFile",
                    "NoConfigFile",
                    "ParentUnavailable",
                    "SharedConfigFile",
                }:
                    raise ConfigEditError(code, 409)
                raise ConfigEditError("ApplyRejected", 503)
            if reply.result.get("status") != "accepted":
                raise ConfigEditError("ApplyRejected", 503)
            return _answer(
                {"ok": True, "data": {"status": "accepted"}}, status_code=202
            )
        except ValueError, TypeError, json.JSONDecodeError:
            return _answer(
                {"ok": False, "error": "InvalidEdit"}, status_code=400
            )
        except ConfigEditError as exc:
            return _answer(
                {"ok": False, "error": exc.code}, status_code=exc.status
            )
        except Exception:
            return _answer(
                {"ok": False, "error": "ApplyUnavailable"}, status_code=503
            )

    return [
        Route(api_prefix + "/config", inspect, methods=["GET"]),
        Route(api_prefix + "/config", save, methods=["POST"]),
        Route(api_prefix + "/config/apply", apply, methods=["POST"]),
    ]
