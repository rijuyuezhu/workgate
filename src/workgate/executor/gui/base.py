import asyncio
import base64
import contextlib
import json
import math
import os
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from mcp.types import CallToolResult, ContentBlock, ImageContent, TextContent
from PIL import Image

from ...config.executor import ExecutorConfig
from ...errors import GuiStaleStateError, GuiUnavailableError
from ...utils.image_types import detect_image_type
from ..tool_session.store import ToolSessionStore

GUI_STATE_TTL_S = 30.0
GUI_STATE_CACHE_LIMIT = 32
GUI_MAX_ELEMENTS = 1000
GUI_MAX_DEPTH = 20
GUI_MAX_ACTIONS = 32
GUI_MAX_TOTAL_WAIT_S = 30.0
GUI_MAX_TEXT_BYTES = 4096
GUI_MAX_KEY_PARTS = 16
GUI_MAX_KEYS_BYTES = 256
GUI_MAX_WINDOWS = 256
GUI_MAX_WINDOW_ID_BYTES = 1024
GUI_MAX_WINDOW_TEXT_BYTES = 1024
GUI_MAX_WINDOWS_TOTAL_BYTES = 128 * 1024
GUI_MAX_CAPTURE_DIMENSION = 16_384
GUI_MAX_CAPTURE_PIXELS = 64_000_000
GUI_MAX_ELEMENT_TEXT_BYTES = 1024
GUI_MAX_ELEMENT_VALUE_BYTES = 2048
GUI_MAX_ELEMENTS_TOTAL_BYTES = 64 * 1024

_COORDINATE_ACTIONS = {
    "click",
    "double_click",
    "right_click",
    "move",
    "scroll",
    "drag",
}
_HUMAN_ACTIONS = _COORDINATE_ACTIONS | {"type", "key"}
_GUI_ACTIONS = _HUMAN_ACTIONS | {"set_value", "focus", "wait"}


def _assert_action_fresh(
    action: dict[str, Any],
    *,
    stale_hint: str = "refresh the GUI observation and try again",
) -> None:
    raw_deadline = action.get("_observation_deadline")
    if raw_deadline is None:
        return
    try:
        deadline = float(raw_deadline)
    except TypeError, ValueError:
        raise GuiStaleStateError(
            f"GUI observation deadline is invalid; {stale_hint}"
        ) from None
    if time.monotonic() > deadline:
        raise GuiStaleStateError(
            f"GUI observation expired while preparing input; {stale_hint}"
        )


@dataclass(slots=True)
class GuiSnapshot:
    window: dict[str, Any]
    elements: list[dict[str, Any]]
    locators: dict[str, Any] = field(default_factory=dict)
    screenshot_path: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)


class GuiBackend(Protocol):
    name: str

    async def list_windows(self) -> dict[str, Any]: ...

    async def snapshot(
        self,
        window_id: str,
        *,
        screenshot_path: Path | None,
        include_elements: bool,
        max_elements: int,
        max_depth: int,
    ) -> GuiSnapshot: ...

    async def perform_action(
        self,
        window: dict[str, Any],
        locator: Any | None,
        action: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def focus_window(self, window: dict[str, Any]) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class _StateRecord:
    state_id: str
    owner_session_id: str
    window: dict[str, Any]
    locators: dict[str, Any]
    created_at: float


def _truncate_gui_text(value: Any, limit: int) -> str:
    text = str(value or "")
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    suffix = "..."
    budget = max(0, limit - len(suffix))
    return encoded[:budget].decode("utf-8", errors="ignore") + suffix


def _bounded_element_record(element: dict[str, Any]) -> dict[str, Any]:
    bounded: dict[str, Any] = {}
    for key in ("id", "role", "name", "automation_id"):
        if key in element:
            bounded[key] = _truncate_gui_text(
                element.get(key), GUI_MAX_ELEMENT_TEXT_BYTES
            )
    for key in ("value", "description"):
        if key in element:
            bounded[key] = _truncate_gui_text(
                element.get(key), GUI_MAX_ELEMENT_VALUE_BYTES
            )
    for key in ("enabled", "focused", "editable", "offscreen", "depth"):
        if key in element:
            bounded[key] = element.get(key)
    bounds = element.get("bounds")
    if isinstance(bounds, dict):
        bounded["bounds"] = {
            key: bounds.get(key)
            for key in ("x", "y", "width", "height")
            if key in bounds
        }
    actions = element.get("actions")
    if isinstance(actions, list):
        bounded["actions"] = [
            _truncate_gui_text(item, GUI_MAX_ELEMENT_TEXT_BYTES)
            for item in actions[:32]
        ]
    return bounded


def _bounded_elements(
    elements: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    bounded: list[dict[str, Any]] = []
    kept_ids: set[str] = set()
    used = 2
    for raw in elements[:GUI_MAX_ELEMENTS]:
        if not isinstance(raw, dict):
            continue
        record = _bounded_element_record(raw)
        encoded = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        extra = len(encoded) + (1 if bounded else 0)
        if used + extra > GUI_MAX_ELEMENTS_TOTAL_BYTES:
            break
        bounded.append(record)
        used += extra
        element_id = record.get("id")
        if isinstance(element_id, str):
            kept_ids.add(element_id)
    return bounded, kept_ids


def _bounded_window_record(window: dict[str, Any]) -> dict[str, Any] | None:
    bounded: dict[str, Any] = {}
    if "id" in window:
        window_id = str(window.get("id") or "")
        if len(window_id.encode("utf-8")) > GUI_MAX_WINDOW_ID_BYTES:
            return None
        bounded["id"] = window_id
    for key in ("title", "app"):
        if key in window:
            bounded[key] = _truncate_gui_text(
                window.get(key), GUI_MAX_WINDOW_TEXT_BYTES
            )
    if "pid" in window:
        raw_pid = window.get("pid")
        try:
            bounded["pid"] = int(raw_pid) if raw_pid is not None else 0
        except TypeError, ValueError:
            bounded["pid"] = 0
    bounds = window.get("bounds")
    if isinstance(bounds, dict):
        bounded["bounds"] = {
            key: bounds.get(key)
            for key in ("x", "y", "width", "height")
            if key in bounds
        }
    return bounded


def _bounded_window_records(windows: Any) -> list[dict[str, Any]]:
    if not isinstance(windows, list):
        return []
    bounded: list[dict[str, Any]] = []
    used = 2
    for raw in windows[:GUI_MAX_WINDOWS]:
        if not isinstance(raw, dict):
            continue
        record = _bounded_window_record(raw)
        if record is None:
            continue
        encoded = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        extra = len(encoded) + (1 if bounded else 0)
        if used + extra > GUI_MAX_WINDOWS_TOTAL_BYTES:
            break
        bounded.append(record)
        used += extra
    return bounded


def _normalize_screenshot_coordinates(
    path: Path, window: dict[str, Any]
) -> None:
    bounds = window.get("bounds")
    if not isinstance(bounds, dict):
        return
    try:
        width = int(bounds["width"])
        height = int(bounds["height"])
    except KeyError, TypeError, ValueError:
        return
    if width <= 0 or height <= 0:
        return
    if (
        width > GUI_MAX_CAPTURE_DIMENSION
        or height > GUI_MAX_CAPTURE_DIMENSION
        or width * height > GUI_MAX_CAPTURE_PIXELS
    ):
        raise GuiUnavailableError(
            f"GUI window dimensions exceed the safe screenshot budget: {width}x{height}"
        )
    with Image.open(path) as image:
        image_width, image_height = image.size
        if (
            image_width <= 0
            or image_height <= 0
            or image_width > GUI_MAX_CAPTURE_DIMENSION
            or image_height > GUI_MAX_CAPTURE_DIMENSION
            or image_width * image_height > GUI_MAX_CAPTURE_PIXELS
        ):
            raise GuiUnavailableError(
                "Captured GUI image exceeds the safe screenshot budget"
            )
        image.load()
        if image.size == (width, height):
            return
        normalized = image.resize((width, height), Image.Resampling.LANCZOS)
        normalized.save(path, format="PNG")


def _bounds_tuple(value: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(value, dict):
        return None
    try:
        return (
            int(value["x"]),
            int(value["y"]),
            int(value["width"]),
            int(value["height"]),
        )
    except KeyError, TypeError, ValueError:
        return None


def _prepare_gui_screenshot_path(prefix: str) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix=f"workgate-{prefix}-", suffix=".png")
    os.close(fd)
    path = Path(raw_path)
    try:
        path.chmod(0o600)
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise GuiUnavailableError(
            "Could not secure the GUI screenshot file"
        ) from exc
    return path


def _secure_gui_screenshot_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError as exc:
        raise GuiUnavailableError(
            "Could not secure the GUI screenshot file"
        ) from exc


def _window_relative_elements(
    elements: list[dict[str, Any]],
    window: dict[str, Any],
) -> list[dict[str, Any]]:
    window_bounds = _bounds_tuple(window.get("bounds"))
    if window_bounds is None:
        return [dict(element) for element in elements]
    window_x, window_y, _width, _height = window_bounds
    normalized = []
    for element in elements:
        item = dict(element)
        bounds = _bounds_tuple(element.get("bounds"))
        if bounds is not None:
            x, y, width, height = bounds
            item["bounds"] = {
                "x": x - window_x,
                "y": y - window_y,
                "width": width,
                "height": height,
            }
        normalized.append(item)
    return normalized


def _validate_window_relative_point(
    window: dict[str, Any],
    x: Any,
    y: Any,
    *,
    label: str,
) -> None:
    bounds = _bounds_tuple(window.get("bounds"))
    if bounds is None:
        raise ValueError("Target window has invalid bounds")
    _window_x, _window_y, width, height = bounds
    try:
        px = int(x)
        py = int(y)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{label} requires integer x and y coordinates"
        ) from exc
    if px < 0 or py < 0 or px >= width or py >= height:
        raise ValueError(
            f"{label} ({px}, {py}) is outside the selected window "
            f"({width}x{height})"
        )


def _validate_coordinate_action(
    window: dict[str, Any],
    action: dict[str, Any],
    *,
    has_locator: bool,
) -> None:
    kind = str(action["type"])
    has_x = action.get("x") is not None
    has_y = action.get("y") is not None
    if has_x != has_y:
        raise ValueError(
            f"{kind} requires both x and y when either coordinate is provided"
        )
    if has_x:
        _validate_window_relative_point(
            window,
            action["x"],
            action["y"],
            label=f"{kind} point",
        )
    elif not has_locator:
        raise ValueError(f"{kind} requires x and y, or an element_id")

    if kind == "drag":
        if action.get("to_x") is None or action.get("to_y") is None:
            raise ValueError("drag requires to_x and to_y")
        _validate_window_relative_point(
            window,
            action["to_x"],
            action["to_y"],
            label="drag destination",
        )


def quantize_scroll_amount(value: Any, *, limit: int = 100) -> int:
    amount = float(value)
    if amount == 0:
        return 0
    magnitude = max(1, min(math.ceil(abs(amount)), limit))
    return -magnitude if amount < 0 else magnitude


async def _await_native_operation(awaitable: Any) -> Any:
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except BaseException:
        with contextlib.suppress(BaseException):
            await asyncio.shield(task)
        raise


def _cleanup_gui_screenshot(path: Path) -> None:
    path.unlink(missing_ok=True)


class GuiService:
    """Own bounded native desktop observations for Workgate execution sessions."""

    def __init__(
        self,
        config: ExecutorConfig,
        store: ToolSessionStore,
        backend: GuiBackend | None = None,
        *,
        backend_factory: Callable[[], GuiBackend] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._backend = backend
        self._backend_factory = backend_factory
        self._owner_backends: dict[str, GuiBackend] = {}
        self._states: dict[str, _StateRecord] = {}
        self._lock = asyncio.Lock()
        self._execution_lock = asyncio.Lock()
        self._closed = False

    def _native_backend(self, owner_session_id: str) -> GuiBackend:
        if self._closed:
            raise RuntimeError("GUI service is closed")
        shared = self._backend
        if shared is not None:
            return shared
        backend = self._owner_backends.get(owner_session_id)
        if backend is not None:
            return backend
        factory = self._backend_factory
        if factory is None:
            raise GuiUnavailableError("native GUI backend is not configured")
        backend = factory()
        self._owner_backends[owner_session_id] = backend
        return backend

    def _touch_owner(self, owner_session_id: str) -> None:
        self._store.touch_session(owner_session_id)

    async def list_windows(self, owner_session_id: str) -> dict[str, Any]:
        self._touch_owner(owner_session_id)
        async with self._execution_lock:
            backend = self._native_backend(owner_session_id)
            result = await _await_native_operation(backend.list_windows())
        result = dict(result)
        capabilities = result.get("capabilities")
        self._touch_owner(owner_session_id)
        return {
            "session_id": owner_session_id,
            "backend": backend.name,
            "windows": _bounded_window_records(result.get("windows")),
            "capabilities": capabilities
            if isinstance(capabilities, dict)
            else {},
        }

    async def snapshot(
        self,
        owner_session_id: str,
        window_id: str,
        *,
        screenshot: bool = True,
        include_elements: bool = True,
        max_elements: int = 300,
        max_depth: int = 12,
    ) -> CallToolResult:
        self._touch_owner(owner_session_id)
        max_elements = max(1, min(int(max_elements), GUI_MAX_ELEMENTS))
        max_depth = max(1, min(int(max_depth), GUI_MAX_DEPTH))
        screenshot_path = (
            _prepare_gui_screenshot_path("gui") if screenshot else None
        )
        image_content: ImageContent | None = None
        screenshot_meta: dict[str, Any] | None = None
        try:
            async with self._execution_lock:
                backend = self._native_backend(owner_session_id)
                try:
                    snapshot = await _await_native_operation(
                        backend.snapshot(
                            str(window_id),
                            screenshot_path=screenshot_path,
                            include_elements=include_elements,
                            max_elements=max_elements,
                            max_depth=max_depth,
                        )
                    )
                except LookupError as exc:
                    raise GuiStaleStateError(str(exc)) from exc
                captured_at = time.monotonic()

                if screenshot_path is not None:
                    if (
                        snapshot.screenshot_path is None
                        or not screenshot_path.is_file()
                        or screenshot_path.stat().st_size <= 0
                    ):
                        raise GuiUnavailableError(
                            "GUI backend did not produce the requested screenshot"
                        )
                    _secure_gui_screenshot_file(screenshot_path)
                    await asyncio.to_thread(
                        _normalize_screenshot_coordinates,
                        screenshot_path,
                        snapshot.window,
                    )
                    image_size = screenshot_path.stat().st_size
                    if image_size > self._config.max_view_image_bytes:
                        raise GuiUnavailableError(
                            "GUI screenshot exceeds the configured native image limit"
                        )
                    image_data = await asyncio.to_thread(
                        screenshot_path.read_bytes
                    )
                    if len(image_data) != image_size:
                        raise GuiUnavailableError(
                            "GUI screenshot changed while it was being read"
                        )
                    _image_format, mime_type = detect_image_type(
                        image_data[:16]
                    )
                    image_content = ImageContent(
                        type="image",
                        data=base64.b64encode(image_data).decode("ascii"),
                        mimeType=mime_type,
                    )
                    screenshot_meta = {
                        "mime_type": mime_type,
                        "bytes": image_size,
                    }

                bounded_elements, kept_ids = _bounded_elements(
                    snapshot.elements
                )
                bounded_locators = {
                    element_id: locator
                    for element_id, locator in snapshot.locators.items()
                    if element_id in kept_ids
                }
                bounded_window = _bounded_window_record(snapshot.window)
                if bounded_window is None:
                    raise GuiUnavailableError(
                        "GUI backend returned unsafe window metadata"
                    )
                if str(bounded_window.get("id") or "") != str(window_id):
                    raise GuiStaleStateError(
                        "GUI backend returned a different window than requested; "
                        "call gui_state again"
                    )
                state_id = uuid.uuid4().hex
                now = time.monotonic()
                remaining_ttl = max(0.0, GUI_STATE_TTL_S - (now - captured_at))
                if remaining_ttl <= 0:
                    raise GuiStaleStateError(
                        "GUI observation expired while preparing the captured "
                        "state; call gui_state again"
                    )
                async with self._lock:
                    self._prune_locked(now)
                    if len(self._states) >= GUI_STATE_CACHE_LIMIT:
                        raise GuiUnavailableError(
                            "Too many active GUI observations; retry after an "
                            "existing state expires or is consumed"
                        )
                    self._states[state_id] = _StateRecord(
                        state_id=state_id,
                        owner_session_id=owner_session_id,
                        window=dict(snapshot.window),
                        locators=bounded_locators,
                        created_at=captured_at,
                    )

            metadata = {
                "session_id": owner_session_id,
                "backend": backend.name,
                "state_id": state_id,
                "state_ttl_s": remaining_ttl,
                "window": bounded_window,
                "elements": _window_relative_elements(
                    bounded_elements, bounded_window
                ),
                "capabilities": snapshot.capabilities,
                "screenshot": screenshot_meta,
            }
            content: list[ContentBlock] = []
            if image_content is not None:
                content.append(image_content)
            content.append(
                TextContent(
                    type="text",
                    text=(
                        f"GUI state {state_id} for window "
                        f"{bounded_window.get('id', window_id)} "
                        f"(valid for {remaining_ttl:.1f}s)"
                    ),
                )
            )
            self._touch_owner(owner_session_id)
            return CallToolResult(
                content=content,
                structuredContent=metadata,
            )
        finally:
            if screenshot_path is not None:
                _cleanup_gui_screenshot(screenshot_path)

    async def act(
        self,
        owner_session_id: str,
        window_id: str,
        state_id: str,
        actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self._touch_owner(owner_session_id)
        self._validate_action_batch(actions)

        results: list[dict[str, Any]] = []
        async with self._execution_lock:
            backend = self._native_backend(owner_session_id)
            async with self._lock:
                now = time.monotonic()
                self._prune_locked(now)
                record = self._states.get(state_id)
                if record is None:
                    raise GuiStaleStateError(
                        "GUI state is stale, unknown, or already consumed; "
                        "call gui_state again"
                    )
                if record.owner_session_id != owner_session_id:
                    raise GuiStaleStateError(
                        "GUI state belongs to a different Workgate session; "
                        "call gui_state again"
                    )
                if str(record.window.get("id")) != str(window_id):
                    raise GuiStaleStateError(
                        "GUI state belongs to a different window; "
                        "call gui_state again"
                    )

                normalized: list[tuple[dict[str, Any], Any | None]] = []
                for raw_action in actions:
                    action = dict(raw_action)
                    kind = str(action.get("type") or "").strip().lower()
                    action["type"] = kind
                    target = action.get("element_id")
                    locator = None
                    if target is not None:
                        locator = record.locators.get(str(target))
                        if locator is None:
                            raise ValueError(
                                f"Unknown element_id {target!r} "
                                f"for state {state_id}"
                            )
                    if kind in _COORDINATE_ACTIONS:
                        _validate_coordinate_action(
                            record.window,
                            action,
                            has_locator=locator is not None,
                        )
                    normalized.append((action, locator))
                self._states.pop(state_id, None)

            for index, (action, locator) in enumerate(normalized):
                if time.monotonic() - record.created_at > GUI_STATE_TTL_S:
                    raise GuiStaleStateError(
                        "GUI state expired during action batch; "
                        "call gui_state again"
                    )
                action["_observation_deadline"] = (
                    record.created_at + GUI_STATE_TTL_S
                )
                try:
                    if action["type"] in _COORDINATE_ACTIONS:
                        await self._assert_window_geometry_unchanged(
                            backend, record.window
                        )
                        await _await_native_operation(
                            backend.focus_window(record.window)
                        )
                        await self._assert_window_geometry_unchanged(
                            backend, record.window
                        )
                        action["_focus_prepared"] = True
                    _assert_action_fresh(
                        action, stale_hint="call gui_state again"
                    )
                    result = await _await_native_operation(
                        backend.perform_action(record.window, locator, action)
                    )
                except LookupError as exc:
                    raise GuiStaleStateError(str(exc)) from exc
                results.append(
                    {
                        "index": index,
                        "type": action["type"],
                        **(result or {}),
                    }
                )

        self._touch_owner(owner_session_id)
        return {
            "session_id": owner_session_id,
            "backend": backend.name,
            "state_id": state_id,
            "window_id": str(window_id),
            "state_consumed": True,
            "actions": results,
        }

    async def discard_owner(self, owner_session_id: str) -> int:
        async with self._execution_lock:
            async with self._lock:
                owned = [
                    state_id
                    for state_id, record in self._states.items()
                    if record.owner_session_id == owner_session_id
                ]
                for state_id in owned:
                    self._states.pop(state_id, None)
            backend = self._owner_backends.pop(owner_session_id, None)
            if backend is not None:
                await backend.aclose()
        return len(owned)

    async def aclose(self) -> None:
        self._closed = True
        async with self._execution_lock:
            async with self._lock:
                self._states.clear()
            backends = [
                backend
                for backend in [self._backend, *self._owner_backends.values()]
                if backend is not None
            ]
            self._backend = None
            self._owner_backends.clear()
            closed: set[int] = set()
            for backend in backends:
                identity = id(backend)
                if identity in closed:
                    continue
                closed.add(identity)
                await backend.aclose()

    async def _current_window(
        self,
        backend: GuiBackend,
        window_id: str,
    ) -> dict[str, Any]:
        current = await backend.list_windows()
        wanted = str(window_id)
        match = next(
            (
                item
                for item in current.get("windows", [])
                if str(item.get("id")) == wanted
            ),
            None,
        )
        if match is None:
            raise GuiStaleStateError(
                "Target window is no longer available; call gui_state again"
            )
        return match

    async def _assert_window_geometry_unchanged(
        self,
        backend: GuiBackend,
        observed: dict[str, Any],
    ) -> None:
        match = await self._current_window(backend, str(observed.get("id")))
        old_bounds = _bounds_tuple(observed.get("bounds"))
        new_bounds = _bounds_tuple(match.get("bounds"))
        if (
            old_bounds is not None
            and new_bounds is not None
            and old_bounds != new_bounds
        ):
            raise GuiStaleStateError(
                "Target window moved or resized; call gui_state again"
            )

    @staticmethod
    def _validate_action_batch(actions: list[dict[str, Any]]) -> None:
        if not actions:
            raise ValueError("actions must contain at least one GUI action")
        if len(actions) > GUI_MAX_ACTIONS:
            raise ValueError(
                f"actions may contain at most {GUI_MAX_ACTIONS} GUI actions"
            )
        total_wait = 0.0
        for index, raw_action in enumerate(actions):
            kind = str(raw_action.get("type") or "").strip().lower()
            if not kind:
                raise ValueError(f"actions[{index}].type is required")
            if kind not in _GUI_ACTIONS:
                raise ValueError(f"Unsupported GUI action type: {kind}")
            target = raw_action.get("element_id")
            if target is not None and not str(target).strip():
                raise ValueError(
                    f"actions[{index}].element_id must not be empty"
                )
            if kind in _COORDINATE_ACTIONS:
                has_x = raw_action.get("x") is not None
                has_y = raw_action.get("y") is not None
                if has_x != has_y:
                    raise ValueError(
                        f"{kind} requires both x and y when either "
                        "coordinate is provided"
                    )
                if target is None and not has_x:
                    raise ValueError(
                        f"actions[{index}] requires x and y, or an element_id"
                    )
                if kind == "drag" and (
                    raw_action.get("to_x") is None
                    or raw_action.get("to_y") is None
                ):
                    raise ValueError(
                        f"actions[{index}] drag requires to_x and to_y"
                    )
            if kind == "type" and raw_action.get("text") is None:
                raise ValueError(f"actions[{index}].text is required for type")
            if kind == "key" and raw_action.get("keys") is None:
                raise ValueError(f"actions[{index}].keys is required for key")
            if kind == "set_value" and target is None:
                raise ValueError(
                    f"actions[{index}].element_id is required for set_value"
                )
            text = raw_action.get("text")
            if (
                text is not None
                and len(str(text).encode("utf-8")) > GUI_MAX_TEXT_BYTES
            ):
                raise ValueError(
                    f"actions[{index}].text may not exceed "
                    f"{GUI_MAX_TEXT_BYTES} UTF-8 bytes"
                )
            keys = raw_action.get("keys")
            if keys is not None:
                if isinstance(keys, str):
                    key_parts = [
                        part.strip()
                        for part in keys.replace("+", " ").split()
                        if part.strip()
                    ]
                    key_bytes = len(keys.encode("utf-8"))
                elif isinstance(keys, list):
                    key_parts = [
                        str(part).strip() for part in keys if str(part).strip()
                    ]
                    key_bytes = sum(
                        len(part.encode("utf-8")) for part in key_parts
                    )
                else:
                    raise ValueError(
                        f"actions[{index}].keys must be a string or list"
                    )
                if not key_parts:
                    raise ValueError(
                        f"actions[{index}].keys must contain at least one key"
                    )
                if len(key_parts) > GUI_MAX_KEY_PARTS:
                    raise ValueError(
                        f"actions[{index}].keys may contain at most "
                        f"{GUI_MAX_KEY_PARTS} parts"
                    )
                if key_bytes > GUI_MAX_KEYS_BYTES:
                    raise ValueError(
                        f"actions[{index}].keys may not exceed "
                        f"{GUI_MAX_KEYS_BYTES} UTF-8 bytes"
                    )
            if kind != "wait":
                continue
            try:
                seconds = float(raw_action.get("seconds", 1.0))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"actions[{index}].seconds must be numeric"
                ) from exc
            total_wait += max(0.0, min(seconds, 30.0))
        if total_wait > GUI_MAX_TOTAL_WAIT_S:
            raise ValueError(
                "Total GUI wait time may not exceed "
                f"{GUI_MAX_TOTAL_WAIT_S:g} seconds"
            )

    def _prune_locked(self, now: float) -> None:
        expired = [
            state_id
            for state_id, record in self._states.items()
            if now - record.created_at > GUI_STATE_TTL_S
        ]
        for state_id in expired:
            self._states.pop(state_id, None)


def display_screenshot_path(path: Path) -> str:
    """Return an executor-local screenshot path for backend bookkeeping only."""
    return str(path)
