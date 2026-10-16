"""Authenticated owner administration for final executors and pairing."""

import asyncio
from typing import Any, cast

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from ...agent_bridge.diagnostics import manifest_diagnostics
from ...audit import audit
from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import SCOPE_EXECUTOR_USE
from ...protocol.pairing import PairApprovalRequest
from ..executors import ControlExecutorFleetService, ExecutorNotFoundError
from ..pairing import (
    ExecutorPairingError,
    ExecutorPairingService,
    PairingAttemptView,
)


class _RenameExecutorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executor_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=80)


class _RevokeExecutorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executor_id: str = Field(min_length=1, max_length=128)


def _json_ok(data: Any = None, message: str = "") -> JSONResponse:
    return JSONResponse(
        {"ok": True, "message": message, "data": data},
        headers={"Cache-Control": "no-store"},
    )


def _json_error(error: str, message: str, *, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": error, "message": message},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _require_executor_admin_scope() -> None:
    try:
        require_oauth_scopes((SCOPE_EXECUTOR_USE,))
    except MissingOAuthScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _normalize_user_code(value: str | None) -> str:
    return (value or "").strip().upper()


def _pairing_view(view: PairingAttemptView) -> dict[str, Any]:
    return {
        "user_code": view.user_code,
        "requested_name": view.requested_name,
        "existing_executor_id": view.existing_executor_id,
        "metadata": view.metadata.model_dump(mode="json"),
        "expires_in": view.expires_in,
        "status": view.status,
        "executor_id": view.executor_id,
        "name": view.name,
    }


def executor_admin_routes(
    fleet: ControlExecutorFleetService,
    pairing: ExecutorPairingService,
    *,
    api_prefix: str,
) -> list[BaseRoute]:
    """Return owner-authenticated final executor administration routes."""

    async def pair(request: Request) -> Response:
        _require_executor_admin_scope()
        try:
            if request.method == "GET":
                code = _normalize_user_code(request.query_params.get("code"))
                if not code:
                    return _json_error(
                        "PairingCodeRequired",
                        "Pairing user code is required",
                        status_code=400,
                    )
                view = await pairing.lookup_user_code(code)
                return _json_ok(_pairing_view(view))

            raw = await request.json()
            approval = PairApprovalRequest.model_validate(raw)
            view = await pairing.decide(approval)
            audit(
                "executor_pairing_decided",
                decision=approval.decision.value,
                executor_id=view.executor_id,
                name=view.name,
                replacement=approval.replace_executor_id is not None,
            )
            return _json_ok(_pairing_view(view), "Pairing decision recorded")
        except ExecutorPairingError as exc:
            return _json_error(
                exc.error.code.value,
                exc.error.message,
                status_code=404
                if exc.error.code.value
                in {"pairing_required", "pairing_expired"}
                else 409,
            )
        except ValidationError as exc:
            return _json_error(
                "InvalidPairingDecision", str(exc), status_code=400
            )
        except TypeError, ValueError:
            return _json_error(
                "InvalidPairingDecision",
                "Pairing decision body is invalid",
                status_code=400,
            )

    async def executors(_request: Request) -> Response:
        _require_executor_admin_scope()
        result = await fleet.list()
        return _json_ok(
            {
                "executors": [
                    entry.model_dump(mode="json") for entry in result.executors
                ],
                "session_capacity": None
                if result.session_capacity is None
                else result.session_capacity.model_dump(mode="json"),
                "bootstrap": None
                if result.bootstrap is None
                else result.bootstrap.model_dump(mode="json"),
            }
        )

    async def diagnostics(request: Request) -> Response:
        """Authorized, on-demand Executor diagnostics; never serve raw config."""
        _require_executor_admin_scope()
        executor_id = (request.query_params.get("executor_id") or "").strip()
        if not executor_id or len(executor_id.encode("utf-8")) > 128:
            return _json_error(
                "InvalidExecutorId", "Invalid executor_id", status_code=400
            )
        runtime = getattr(request.app.state, "control_runtime", None)
        if runtime is None:
            return _json_error(
                "Unavailable", "Control is unavailable", status_code=503
            )
        record = runtime.control_state.snapshot_executors().get(executor_id)
        if record is None or record.revoked_at is not None:
            return _json_error(
                "ExecutorNotFound", "Executor does not exist", status_code=404
            )
        control = await asyncio.to_thread(
            manifest_diagnostics,
            runtime.config.agent_config_dir,
            owner="control",
        )
        control["enabled"] = runtime.config.agent_bridge_enabled
        online = await runtime.executor_transport.is_online(executor_id)
        if not online:
            return _json_ok(
                {
                    "executor_id": executor_id,
                    "status": "offline",
                    "control_mcp": control,
                    "executor": None,
                }
            )
        try:
            result = await runtime.executor_transport.call(
                executor_id, "ui.executor_diagnostics", {}, timeout_s=8
            )
            if not result.ok or not isinstance(result.result, dict):
                raise RuntimeError("diagnostic RPC unavailable")
            payload = cast(dict[str, Any], result.result)

            # Allowlist data from a peer: a faulty or mismatched Executor must
            # never reflect raw secrets, env, manifest or file paths to UI.
            def rows(
                values: Any, *, fields: tuple[str, ...]
            ) -> list[dict[str, Any]]:
                if not isinstance(values, list):
                    return []
                selected = []
                for value in values[:32]:
                    if not isinstance(value, dict):
                        continue
                    row = {}
                    for key in fields:
                        field = value.get(key)
                        if key in {"enabled", "available"}:
                            if type(field) is bool:
                                row[key] = field
                        elif key == "type":
                            if isinstance(field, str) and field in {
                                "http",
                                "stdio",
                            }:
                                row[key] = field
                        elif key == "source":
                            if isinstance(field, str) and field in {
                                "managed",
                                "global",
                                "project",
                                "control",
                            }:
                                row[key] = field
                        elif isinstance(field, str):
                            row[key] = field[:80]
                    selected.append(row)
                return selected

            raw_bridge = payload.get("bridge")
            raw_skills = payload.get("skills")
            bridge: dict[str, Any] = (
                cast(dict[str, Any], raw_bridge)
                if isinstance(raw_bridge, dict)
                else {}
            )
            skill: dict[str, Any] = (
                cast(dict[str, Any], raw_skills)
                if isinstance(raw_skills, dict)
                else {}
            )
            snapshot = {
                "bridge": {
                    "status": bridge.get("status")
                    if isinstance(bridge.get("status"), str)
                    and bridge["status"]
                    in {
                        "loaded",
                        "missing_config",
                        "invalid_config",
                        "unavailable",
                    }
                    else "unavailable",
                    "errors": min(100, bridge.get("errors", 0))
                    if type(bridge.get("errors")) is int
                    and bridge["errors"] >= 0
                    else 0,
                    "servers": rows(
                        bridge.get("servers"),
                        fields=("name", "type", "enabled"),
                    ),
                    "truncated": bridge.get("truncated") is True,
                },
                "skills": {
                    "enabled": skill.get("enabled") is True,
                    "unavailable": skill.get("unavailable") is True,
                    "selected": rows(
                        skill.get("selected"), fields=("name", "source")
                    ),
                    "overridden": rows(
                        skill.get("overridden"), fields=("name", "source")
                    ),
                    "warnings": min(100, skill.get("warnings", 0))
                    if type(skill.get("warnings")) is int
                    and skill["warnings"] >= 0
                    else 0,
                    "duplicates": min(100, skill.get("duplicates", 0))
                    if type(skill.get("duplicates")) is int
                    and skill["duplicates"] >= 0
                    else 0,
                },
            }
            return _json_ok(
                {
                    "executor_id": executor_id,
                    "status": "available",
                    "control_mcp": control,
                    "executor": snapshot,
                }
            )
        except ConnectionError, RuntimeError, TimeoutError, ValueError:
            return _json_ok(
                {
                    "executor_id": executor_id,
                    "status": "unavailable",
                    "control_mcp": control,
                    "executor": None,
                }
            )

    async def executor_action(request: Request) -> Response:
        _require_executor_admin_scope()
        action = str(request.path_params.get("action") or "")
        try:
            raw = await request.json()
            if action == "rename":
                body = _RenameExecutorBody.model_validate(raw)
                result = await fleet.execute(
                    action="rename",
                    executor_id=body.executor_id,
                    name=body.name,
                )
                return _json_ok(
                    None
                    if result.executor is None
                    else result.executor.model_dump(mode="json"),
                    "Executor renamed",
                )
            if action in {"reset", "drain", "resume", "revoke"}:
                body = _RevokeExecutorBody.model_validate(raw)
                result = await fleet.execute(
                    action=action,
                    executor_id=body.executor_id,
                )
                data = (
                    {}
                    if result.executor is None
                    else result.executor.model_dump(mode="json")
                )
                if result.cancelled_queued is not None:
                    data["cancelled_queued"] = result.cancelled_queued
                if result.preserved_offered is not None:
                    data["preserved_offered"] = result.preserved_offered
                return _json_ok(data, f"Executor {action} complete")
            return _json_error(
                "UnsupportedExecutorAction",
                f"Unsupported executor action: {action}",
                status_code=400,
            )
        except KeyError, ExecutorNotFoundError:
            return _json_error(
                "ExecutorNotFound", "Executor does not exist", status_code=404
            )
        except ValidationError as exc:
            return _json_error(
                "InvalidExecutorMutation", str(exc), status_code=400
            )
        except ValueError as exc:
            return _json_error(
                "InvalidExecutorMutation", str(exc), status_code=400
            )

    return [
        Route(api_prefix + "/pair", pair, methods=["GET", "POST"]),
        Route(api_prefix + "/executors", executors, methods=["GET"]),
        Route(
            api_prefix + "/executors/diagnostics", diagnostics, methods=["GET"]
        ),
        Route(
            api_prefix + "/executors/{action}",
            executor_action,
            methods=["POST"],
        ),
    ]
