"""Authenticated owner administration for final executors and pairing."""

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

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
            api_prefix + "/executors/{action}",
            executor_action,
            methods=["POST"],
        ),
    ]
