"""Validation and wire contracts for Human UI terminal transports."""

import re
from typing import Any

from pydantic import ValidationError

from ...schemas.result_models.shell import (
    KillPersistentShellOutput,
    ListPersistentShellsOutput,
    ReadPersistentShellOutput,
    StartPersistentShellOutput,
)
from .common import bounded_text as _bounded_text

UI_TERMINAL_READ_MAX_LINES = 5_000
UI_TERMINAL_DEFAULT_LINES = 1_000
UI_TERMINAL_EXECUTOR_MAX_BYTES = 255
UI_TERMINAL_METADATA_MAX_BYTES = 4_096
UI_TERMINAL_OUTPUT_MAX_BYTES = 4_000_000
UI_TERMINAL_MAX_SHELLS = 256
_SHELL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _bounded_int(
    raw: str | int | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
    label: str,
) -> int:
    if raw is None or raw == "":
        value = default
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


def _optional_text(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    normalized = _bounded_text(
        value,
        field=field,
        max_bytes=UI_TERMINAL_METADATA_MAX_BYTES,
    )
    return normalized or None


def _executor_id_arg(value: Any) -> str:
    return _bounded_text(
        value,
        field="executor_id",
        max_bytes=UI_TERMINAL_EXECUTOR_MAX_BYTES,
        allow_empty=False,
    )


def _shell_id(value: Any) -> str:
    shell_id = str(value or "")
    if not _SHELL_ID_PATTERN.fullmatch(shell_id):
        raise ValueError(
            "shell_id must be 1-64 characters using letters, digits, '.', '_' or '-'"
        )
    return shell_id


def _validate_model(model_type: Any, value: Any, *, label: str) -> Any:
    try:
        return model_type.model_validate(value)
    except (ValidationError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Executor returned malformed terminal {label}"
        ) from exc


def _normalize_shell_info(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        raw = value.model_dump(mode="json")
    elif isinstance(value, dict):
        raw = value
    else:
        raise RuntimeError("Executor returned malformed terminal inventory")
    try:
        shell_id = _shell_id(raw.get("shell_id"))
        return {
            "shell_id": shell_id,
            "name": _optional_text(raw.get("name"), field="shell name"),
            "cwd": _optional_text(raw.get("cwd"), field="shell cwd"),
            "command": _optional_text(
                raw.get("command"), field="shell command"
            ),
        }
    except ValueError as exc:
        raise RuntimeError(
            "Executor returned malformed terminal inventory"
        ) from exc


def _normalize_list(executor_id: str, value: Any) -> dict[str, Any]:
    model = _validate_model(
        ListPersistentShellsOutput,
        value,
        label="inventory",
    )
    if len(model.shells) > UI_TERMINAL_MAX_SHELLS:
        raise RuntimeError("Executor returned too many terminal sessions")
    shells = [_normalize_shell_info(item) for item in model.shells]
    identifiers = [item["shell_id"] for item in shells]
    if len(identifiers) != len(set(identifiers)):
        raise RuntimeError("Executor returned duplicate terminal sessions")
    return {
        "executor_id": executor_id,
        "shells": shells,
    }


def _normalize_start(executor_id: str, value: Any) -> dict[str, Any]:
    model = _validate_model(
        StartPersistentShellOutput, value, label="start data"
    )
    try:
        shell_id = _shell_id(model.shell_id)
        return {
            "executor_id": executor_id,
            "shell_id": shell_id,
            "name": _optional_text(model.name, field="shell name"),
            "cwd": _optional_text(model.cwd, field="shell cwd"),
            "command": _optional_text(model.command, field="shell command"),
        }
    except ValueError as exc:
        raise RuntimeError(
            "Executor returned malformed terminal start data"
        ) from exc


def _normalize_read(
    executor_id: str,
    shell_id: str,
    lines: int,
    value: Any,
) -> dict[str, Any]:
    model = _validate_model(ReadPersistentShellOutput, value, label="read data")
    try:
        returned_shell = _shell_id(model.shell_id)
    except ValueError as exc:
        raise RuntimeError(
            "Executor returned malformed terminal read data"
        ) from exc
    if returned_shell != shell_id:
        raise RuntimeError("Executor returned malformed terminal read data")
    output = str(model.output or "")
    if len(output.encode("utf-8")) > UI_TERMINAL_OUTPUT_MAX_BYTES:
        raise RuntimeError("Executor returned oversized terminal output")
    return {
        "executor_id": executor_id,
        "shell_id": returned_shell,
        "output": output,
        "lines": lines,
    }


def _normalize_kill(
    executor_id: str, shell_id: str, value: Any
) -> dict[str, Any]:
    model = _validate_model(KillPersistentShellOutput, value, label="kill data")
    try:
        returned_shell = _shell_id(model.shell_id)
        stderr = _optional_text(model.stderr, field="terminal stderr")
    except ValueError as exc:
        raise RuntimeError(
            "Executor returned malformed terminal kill data"
        ) from exc
    if returned_shell != shell_id:
        raise RuntimeError("Executor returned malformed terminal kill data")
    return {
        "executor_id": executor_id,
        "shell_id": returned_shell,
        "killed": model.killed,
        "stderr": stderr,
    }
