"""Validation and wire contracts for Human UI terminal transports."""

import base64
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError
from starlette.websockets import WebSocket

from ...protocol.terminal import (
    PERSISTENT_SHELL_MAX_COLUMNS,
    PERSISTENT_SHELL_MAX_ROWS,
    PERSISTENT_SHELL_MIN_COLUMNS,
    PERSISTENT_SHELL_MIN_ROWS,
)
from ...schemas.result_models.shell import (
    KillPersistentShellOutput,
    ListPersistentShellsOutput,
    ReadPersistentShellOutput,
    ResizePersistentShellOutput,
    SendPersistentShellInputOutput,
    StartPersistentShellOutput,
)
from .common import bounded_text as _bounded_text

UI_TERMINAL_SUBPROTOCOL = "workgate-ui-terminal"
UI_TERMINAL_INPUT_MAX_BYTES = 65_536
UI_TERMINAL_READ_MAX_LINES = 5_000
UI_TERMINAL_DEFAULT_LINES = 1_000
UI_TERMINAL_POLL_INTERVAL_S = 0.25
UI_TERMINAL_EXECUTOR_POLL_INTERVAL_S = 0.75
UI_TERMINAL_EXECUTOR_MAX_BYTES = 255
UI_TERMINAL_METADATA_MAX_BYTES = 4_096
UI_TERMINAL_OUTPUT_MAX_BYTES = 4_000_000
UI_TERMINAL_MAX_SHELLS = 256
UI_TERMINAL_MODES = frozenset({"snapshot", "auto", "pty"})
_SHELL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass(frozen=True)
class TerminalWebSocketRequest:
    """Validated connection parameters for one Human UI terminal WebSocket."""

    executor_id: str
    """Stable executor identity selected for this terminal attachment."""
    shell_id: str
    """Persistent-shell identifier selected for attachment."""
    requested_mode: str
    """Requested snapshot/auto/pty transport mode."""
    announce_mode: bool
    """Whether the caller explicitly supplied a mode and expects it announced."""
    lines: int
    """Initial bounded snapshot line count."""
    cols: int
    """Validated terminal column count."""
    rows: int
    """Validated terminal row count."""


@dataclass(frozen=True)
class TerminalInputControl:
    """Validated textual terminal input control."""

    kind: Literal["input"]
    """Discriminator for an input control."""
    input_text: str
    """Validated textual input payload."""
    enter: bool
    """Whether a newline should follow textual input."""


@dataclass(frozen=True)
class TerminalResizeControl:
    """Validated terminal resize control."""

    kind: Literal["resize"]
    """Discriminator for a resize control."""
    cols: int
    """Requested validated column count."""
    rows: int
    """Requested validated row count."""


@dataclass(frozen=True)
class TerminalPingControl:
    """Validated keepalive control for one terminal WebSocket."""

    kind: Literal["ping"] = "ping"
    """Discriminator for a ping control."""


@dataclass(frozen=True)
class TerminalCloseControl:
    """Validated request to close one terminal WebSocket attachment."""

    kind: Literal["close"] = "close"
    """Discriminator for a close control."""


type TerminalControl = (
    TerminalInputControl
    | TerminalResizeControl
    | TerminalPingControl
    | TerminalCloseControl
)


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


def _terminal_mode(value: Any) -> str:
    mode = str(value or "snapshot").strip().lower()
    if mode not in UI_TERMINAL_MODES:
        raise ValueError("mode must be one of: snapshot, auto, pty")
    return mode


def parse_terminal_websocket_request(
    *,
    executor_id: Any,
    shell_id: Any,
    mode: Any,
    lines: Any,
    cols: Any,
    rows: Any,
) -> TerminalWebSocketRequest:
    """Validate URL/query inputs for one terminal WebSocket attachment."""
    raw_mode = mode
    return TerminalWebSocketRequest(
        executor_id=_executor_id_arg(executor_id),
        shell_id=_shell_id(shell_id),
        requested_mode=_terminal_mode(raw_mode),
        announce_mode=raw_mode is not None,
        lines=_bounded_int(
            lines,
            default=UI_TERMINAL_DEFAULT_LINES,
            minimum=1,
            maximum=UI_TERMINAL_READ_MAX_LINES,
            label="lines",
        ),
        cols=_bounded_int(
            cols,
            default=120,
            minimum=PERSISTENT_SHELL_MIN_COLUMNS,
            maximum=PERSISTENT_SHELL_MAX_COLUMNS,
            label="cols",
        ),
        rows=_bounded_int(
            rows,
            default=36,
            minimum=PERSISTENT_SHELL_MIN_ROWS,
            maximum=PERSISTENT_SHELL_MAX_ROWS,
            label="rows",
        ),
    )


def parse_terminal_binary_input(value: Any) -> bytes:
    """Validate and bound one binary terminal input frame."""
    raw = bytes(value)
    if len(raw) > UI_TERMINAL_INPUT_MAX_BYTES:
        raise ValueError("Terminal input is too large")
    return raw


def parse_terminal_control(
    text: str,
    *,
    current_cols: int,
    current_rows: int,
) -> TerminalControl:
    """Decode and validate one JSON terminal control frame."""
    if len(text.encode("utf-8")) > UI_TERMINAL_INPUT_MAX_BYTES:
        raise ValueError("Terminal message is too large")
    try:
        control = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Terminal controls must be JSON") from exc
    if not isinstance(control, dict):
        raise ValueError("Terminal control must be an object")

    kind = control.get("type")
    if kind == "input":
        input_text = str(control.get("data") or "")
        enter = bool(control.get("enter", False))
        if len(input_text.encode("utf-8")) > UI_TERMINAL_INPUT_MAX_BYTES:
            raise ValueError("Terminal input is too large")
        return TerminalInputControl(
            kind="input",
            input_text=input_text,
            enter=enter,
        )
    if kind == "resize":
        return TerminalResizeControl(
            kind="resize",
            cols=_bounded_int(
                control.get("cols"),
                default=current_cols,
                minimum=PERSISTENT_SHELL_MIN_COLUMNS,
                maximum=PERSISTENT_SHELL_MAX_COLUMNS,
                label="cols",
            ),
            rows=_bounded_int(
                control.get("rows"),
                default=current_rows,
                minimum=PERSISTENT_SHELL_MIN_ROWS,
                maximum=PERSISTENT_SHELL_MAX_ROWS,
                label="rows",
            ),
        )
    if kind == "ping":
        return TerminalPingControl()
    if kind == "close":
        return TerminalCloseControl()
    raise ValueError("Unsupported terminal control")


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


def _normalize_send(
    executor_id: str, shell_id: str, value: Any
) -> dict[str, Any]:
    model = _validate_model(
        SendPersistentShellInputOutput,
        value,
        label="send data",
    )
    try:
        returned_shell = _shell_id(model.shell_id)
    except ValueError as exc:
        raise RuntimeError(
            "Executor returned malformed terminal send data"
        ) from exc
    if (
        returned_shell != shell_id
        or not 0 <= model.sent_bytes <= UI_TERMINAL_INPUT_MAX_BYTES
    ):
        raise RuntimeError("Executor returned malformed terminal send data")
    return {
        "executor_id": executor_id,
        "shell_id": returned_shell,
        "sent_bytes": model.sent_bytes,
        "enter": model.enter,
    }


def _normalize_resize(
    executor_id: str,
    shell_id: str,
    cols: int,
    rows: int,
    value: Any,
) -> dict[str, Any]:
    model = _validate_model(
        ResizePersistentShellOutput,
        value,
        label="resize data",
    )
    try:
        returned_shell = _shell_id(model.shell_id)
    except ValueError as exc:
        raise RuntimeError(
            "Executor returned malformed terminal resize data"
        ) from exc
    if returned_shell != shell_id or model.cols != cols or model.rows != rows:
        raise RuntimeError("Executor returned malformed terminal resize data")
    return {
        "executor_id": executor_id,
        "shell_id": returned_shell,
        "cols": model.cols,
        "rows": model.rows,
        "resized": model.resized,
        "backend": model.backend,
    }


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


def _websocket_protocols(websocket: WebSocket) -> list[str]:
    raw = websocket.headers.get("sec-websocket-protocol", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _websocket_token(websocket: WebSocket) -> str | None:
    for protocol in _websocket_protocols(websocket):
        if not protocol.startswith("bearer."):
            continue
        encoded = protocol.removeprefix("bearer.")
        if not encoded or len(encoded) > 16_384:
            return None
        padding = "=" * (-len(encoded) % 4)
        try:
            token = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
        except ValueError, UnicodeDecodeError:
            return None
        return token if token else None
    return None
