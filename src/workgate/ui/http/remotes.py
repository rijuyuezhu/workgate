"""Authenticated Human UI control-plane APIs for remote workers."""

import math
import re
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ...audit import audit
from ...config.settings import get_settings
from ...oauth.core.context import MissingOAuthScopeError, require_oauth_scopes
from ...oauth.core.scopes import SCOPE_REMOTE_USE
from ...remote.service import (
    list_remote_machines,
    rename_remote_machine,
    revoke_remote_machine,
)

UI_REMOTE_MAX_MACHINES = 1_024
UI_REMOTE_MACHINE_MAX_CHARS = 128
UI_REMOTE_MACHINE_MAX_BYTES = 512
UI_REMOTE_PATH_MAX_BYTES = 4_096
UI_REMOTE_CAPABILITIES_MAX = 64
UI_REMOTE_CAPABILITY_MAX_BYTES = 128
UI_REMOTE_PROFILE_ID_MAX_BYTES = 128
UI_REMOTE_RECONNECT_COMMAND_MAX_BYTES = 8_192
UI_REMOTE_VERSION_MAX_BYTES = 256
UI_REMOTE_SMALL_INFO_MAX_BYTES = 512
UI_REMOTE_INFO_KEYS = {
    "workgate_version": UI_REMOTE_VERSION_MAX_BYTES,
    "hostname": UI_REMOTE_SMALL_INFO_MAX_BYTES,
    "user": UI_REMOTE_SMALL_INFO_MAX_BYTES,
    "python": UI_REMOTE_SMALL_INFO_MAX_BYTES,
    "platform": UI_REMOTE_SMALL_INFO_MAX_BYTES,
    "cwd": UI_REMOTE_PATH_MAX_BYTES,
    "workdir": UI_REMOTE_PATH_MAX_BYTES,
}
_UI_REMOTE_PROFILE_ID_RE = re.compile(r"p_[A-Za-z0-9_-]{8,64}")


class _RenameBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    machine: str
    new_name: str


class _RevokeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    machine: str


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


def _require_remote_scope() -> None:
    try:
        require_oauth_scopes((SCOPE_REMOTE_USE,))
    except MissingOAuthScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _bounded_text(
    value: Any,
    *,
    field: str,
    max_bytes: int,
    allow_empty: bool = True,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if not text and not allow_empty:
        raise ValueError(f"{field} must not be empty")
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field} exceeds {max_bytes} encoded bytes")
    return text


def _machine_name(value: Any, *, field: str = "machine") -> str:
    name = _bounded_text(
        value,
        field=field,
        max_bytes=UI_REMOTE_MACHINE_MAX_BYTES,
        allow_empty=False,
    )
    if len(name) > UI_REMOTE_MACHINE_MAX_CHARS:
        raise ValueError(
            f"{field} exceeds {UI_REMOTE_MACHINE_MAX_CHARS} characters"
        )
    if any(
        ord(character) < 32 or character in {"/", "\\"} for character in name
    ):
        raise ValueError(f"{field} contains unsupported characters")
    return name


def _optional_path(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    path = _bounded_text(value, field=field, max_bytes=UI_REMOTE_PATH_MAX_BYTES)
    return path or None


def _finite_number(
    value: Any,
    *,
    field: str,
    minimum: float = 0.0,
) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{field} must be finite and at least {minimum}")
    return number


def _nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _capabilities(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("capabilities must be a list")
    if len(value) > UI_REMOTE_CAPABILITIES_MAX:
        raise ValueError(
            f"capabilities exceeds {UI_REMOTE_CAPABILITIES_MAX} entries"
        )
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        capability = _bounded_text(
            item,
            field="capability",
            max_bytes=UI_REMOTE_CAPABILITY_MAX_BYTES,
            allow_empty=False,
        )
        if capability not in seen:
            seen.add(capability)
            output.append(capability)
    return output


def _public_info(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("info must be an object")
    output: dict[str, str] = {}
    for key, limit in UI_REMOTE_INFO_KEYS.items():
        raw = value.get(key)
        if raw is None:
            continue
        output[key] = _bounded_text(
            raw,
            field=f"info.{key}",
            max_bytes=limit,
        )
    return output


def _machine_row(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("remote machine row must be an object")
    status = _bounded_text(
        value.get("status"),
        field="status",
        max_bytes=32,
        allow_empty=False,
    )
    if status not in {"online", "offline"}:
        raise ValueError("status must be online or offline")
    last_seen = _finite_number(value.get("last_seen", 0.0), field="last_seen")
    last_seen_age = value.get("last_seen_age_s")
    offline_after = value.get("offline_after_s")
    raw_profile_id = value.get("profile_id")
    raw_reconnect_command = value.get("reconnect_command")
    if raw_profile_id is None and raw_reconnect_command is None:
        profile_id = None
        reconnect_command = None
    else:
        profile_id = _bounded_text(
            raw_profile_id,
            field="profile_id",
            max_bytes=UI_REMOTE_PROFILE_ID_MAX_BYTES,
            allow_empty=False,
        )
        if not _UI_REMOTE_PROFILE_ID_RE.fullmatch(profile_id):
            raise ValueError("profile_id is invalid")
        reconnect_command = _bounded_text(
            raw_reconnect_command,
            field="reconnect_command",
            max_bytes=UI_REMOTE_RECONNECT_COMMAND_MAX_BYTES,
            allow_empty=False,
        )
    return {
        "name": _machine_name(value.get("name")),
        "status": status,
        "workdir": _optional_path(value.get("workdir"), field="workdir"),
        "profile_id": profile_id,
        "reconnect_command": reconnect_command,
        "last_seen": last_seen,
        "last_seen_age_s": None
        if last_seen_age is None
        else _finite_number(last_seen_age, field="last_seen_age_s"),
        "offline_after_s": None
        if offline_after is None
        else _finite_number(offline_after, field="offline_after_s"),
        "queue_depth": _nonnegative_int(
            value.get("queue_depth") or 0, field="queue_depth"
        ),
        "capabilities": _capabilities(value.get("capabilities", [])),
        "info": _public_info(value.get("info", {})),
    }


def _inventory_payload() -> dict[str, Any]:
    settings = get_settings()
    if not settings.remote_enabled:
        return {
            "enabled": False,
            "machines": [],
            "counts": {"online": 0, "offline": 0, "total": 0},
        }
    raw = list_remote_machines().model_dump(mode="json")
    rows = raw.get("machines")
    if not isinstance(rows, list):
        raise RuntimeError("Remote inventory returned an invalid machine list")
    if len(rows) > UI_REMOTE_MAX_MACHINES:
        raise RuntimeError(
            f"Remote inventory exceeds {UI_REMOTE_MAX_MACHINES} machines"
        )
    machines = [_machine_row(row) for row in rows]
    names = [row["name"] for row in machines]
    if len(names) != len(set(names)):
        raise RuntimeError("Remote inventory contains duplicate machine names")
    online = sum(row["status"] == "online" for row in machines)
    return {
        "enabled": True,
        "machines": machines,
        "counts": {
            "online": online,
            "offline": len(machines) - online,
            "total": len(machines),
        },
    }


def _parse_body(model: type[BaseModel], value: Any) -> BaseModel:
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


def _mutation_disabled() -> JSONResponse:
    return _json_error(
        "RemoteWorkersDisabled",
        "Remote worker support is disabled",
        status_code=409,
    )


async def api_remotes(request: Request) -> Response:
    """List already-enrolled legacy remote workers."""
    _require_remote_scope()
    try:
        return _json_ok(_inventory_payload())
    except Exception:  # noqa: BLE001
        return _json_error(
            "RemoteInventoryUnavailable",
            "Remote worker inventory is unavailable",
            status_code=500,
        )


async def api_remote_action(request: Request) -> Response:
    """Rename or revoke one registered remote worker."""
    _require_remote_scope()
    if not get_settings().remote_enabled:
        return _mutation_disabled()
    action = str(request.path_params.get("action") or "")
    try:
        raw = await request.json()
        if action == "rename":
            body = _parse_body(_RenameBody, raw)
            assert isinstance(body, _RenameBody)
            machine = _machine_name(body.machine)
            new_name = _machine_name(body.new_name, field="new_name")
            result = rename_remote_machine(machine, new_name)
            payload = result.model_dump(mode="json")
            audit(
                "ui_remote_worker_renamed",
                machine=machine,
                new_name=new_name,
            )
            return _json_ok(payload, "Remote worker renamed")
        if action == "revoke":
            body = _parse_body(_RevokeBody, raw)
            assert isinstance(body, _RevokeBody)
            machine = _machine_name(body.machine)
            result = revoke_remote_machine(machine)
            payload = result.model_dump(mode="json")
            audit("ui_remote_worker_revoked", machine=machine)
            return _json_ok(payload, "Remote worker revoked")
        raise ValueError(f"Unsupported remote action: {action}")
    except ValueError as exc:
        return _json_error(type(exc).__name__, str(exc), status_code=400)
    except Exception:  # noqa: BLE001
        return _json_error(
            "RemoteMutationUnavailable",
            "Remote worker operation failed",
            status_code=500,
        )
