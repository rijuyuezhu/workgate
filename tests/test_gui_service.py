import asyncio
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult, ImageContent
from PIL import Image
from pydantic import TypeAdapter, ValidationError

import workgate.executor.gui.base as gui_base
from tests.helpers import (
    build_paired_control_harness,
    build_tool_session_store,
    mcp_structured,
)
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import (
    Settings,
    clear_settings_cache,
    get_settings,
)
from workgate.control.mcp.app import build_mcp
from workgate.errors import GuiStaleStateError, GuiUnavailableError
from workgate.executor.gui.base import GuiService, GuiSnapshot
from workgate.executor.sessions import ExecutorSessionService
from workgate.executor.shell_service import ShellService
from workgate.schemas.input_models.gui import GuiActionsArg


class FakeGuiBackend:
    name = "fake-gui"

    def __init__(self) -> None:
        self.bounds = {"x": 100, "y": 200, "width": 40, "height": 30}
        self.actions: list[tuple[Any, dict[str, Any]]] = []
        self.focuses = 0
        self.closed = False

    def window(self) -> dict[str, Any]:
        return {
            "id": "window-1",
            "title": "Editor",
            "app": "Fake",
            "pid": 123,
            "bounds": dict(self.bounds),
        }

    async def list_windows(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "windows": [self.window()],
            "capabilities": {"semantic_actions": True},
        }

    async def snapshot(
        self,
        window_id: str,
        *,
        screenshot_path: Path | None,
        include_elements: bool,
        max_elements: int,
        max_depth: int,
    ) -> GuiSnapshot:
        assert window_id == "window-1"
        del max_elements, max_depth
        if screenshot_path is not None:
            Image.new("RGB", (40, 30), "white").save(
                screenshot_path, format="PNG"
            )
        elements = (
            [
                {
                    "id": "e1",
                    "role": "button",
                    "name": "Run",
                    "bounds": {"x": 110, "y": 205, "width": 10, "height": 8},
                    "enabled": True,
                }
            ]
            if include_elements
            else []
        )
        return GuiSnapshot(
            window=self.window(),
            elements=elements,
            locators={"e1": {"fingerprint": "stable"}} if elements else {},
            screenshot_path=str(screenshot_path) if screenshot_path else None,
            capabilities={
                "semantic_actions": True,
                "coordinate_space": "window-relative",
            },
        )

    async def focus_window(self, window: dict[str, Any]) -> None:
        assert window["id"] == "window-1"
        self.focuses += 1

    async def perform_action(
        self,
        window: dict[str, Any],
        locator: Any | None,
        action: dict[str, Any],
    ) -> dict[str, Any]:
        assert window["id"] == "window-1"
        self.actions.append((locator, dict(action)))
        return {"performed": True}

    async def aclose(self) -> None:
        self.closed = True


def service(tmp_path: Path) -> tuple[GuiService, FakeGuiBackend, Any, str, str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        default_workdir=workspace,
        state_dir=tmp_path / "state",
        agent_bridge_enabled=False,
    )
    config = resolve_executor_config(settings)
    store = build_tool_session_store(settings)
    first = "sess_0000000000000000000001"
    second = "sess_0000000000000000000002"
    store.create_session(session_id=first, workdir=workspace)
    store.create_session(session_id=second, workdir=workspace)
    backend = FakeGuiBackend()
    return GuiService(config, store, backend), backend, store, first, second


def structured(result) -> dict[str, Any]:
    assert isinstance(result.structured_content, dict)
    return result.structured_content


def test_gui_action_schema_is_closed_and_bounded() -> None:
    adapter = TypeAdapter(GuiActionsArg)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        adapter.validate_python(
            [{"type": "click", "x": 1, "y": 2, "secret": "x"}]
        )
    with pytest.raises(ValidationError, match="Field required"):
        adapter.validate_python([{"type": "drag", "x": 1, "y": 2, "to_x": 3}])
    with pytest.raises(ValidationError, match="at most 32"):
        adapter.validate_python([{"type": "wait"}] * 33)
    with pytest.raises(ValidationError, match="at most 16"):
        adapter.validate_python(
            [{"type": "key", "keys": [str(index) for index in range(17)]}]
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        adapter.validate_python(
            [{"type": "click", "x": 1, "y": 2, "amount": 4}]
        )


@pytest.mark.asyncio
async def test_gui_state_returns_native_image_and_window_relative_elements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, backend, _store, session_id, _second = service(tmp_path)
    capture = tmp_path / "capture.png"
    monkeypatch.setattr(
        gui_base, "_prepare_gui_screenshot_path", lambda _prefix: capture
    )

    result = await gui.snapshot(session_id, "window-1")
    data = structured(result)

    assert data["session_id"] == session_id
    assert data["backend"] == backend.name
    assert data["window"]["bounds"] == {
        "x": 100,
        "y": 200,
        "width": 40,
        "height": 30,
    }
    assert data["elements"][0]["bounds"] == {
        "x": 10,
        "y": 5,
        "width": 10,
        "height": 8,
    }
    assert data["state_ttl_s"] <= 30
    assert data["screenshot"]["mime_type"] == "image/png"
    assert any(isinstance(item, ImageContent) for item in result.content)
    assert not capture.exists()


@pytest.mark.asyncio
async def test_gui_state_rejects_backend_window_identity_change(
    tmp_path: Path,
) -> None:
    gui, backend, _store, first, _second = service(tmp_path)
    original = backend.snapshot

    async def changed_snapshot(*args, **kwargs):
        snapshot = await original(*args, **kwargs)
        snapshot.window["id"] = "window-recycled"
        return snapshot

    backend.snapshot = changed_snapshot

    with pytest.raises(GuiStaleStateError, match="different window"):
        await gui.snapshot(first, "window-1", screenshot=False)

    assert gui._states == {}


@pytest.mark.asyncio
async def test_gui_state_owner_isolation_and_single_use(tmp_path: Path) -> None:
    gui, backend, _store, first, second = service(tmp_path)
    state_id = str(
        structured(await gui.snapshot(first, "window-1", screenshot=False))[
            "state_id"
        ]
    )

    with pytest.raises(GuiStaleStateError, match="different Workgate session"):
        await gui.act(
            second,
            "window-1",
            state_id,
            [{"type": "click", "element_id": "e1"}],
        )

    result = await gui.act(
        first,
        "window-1",
        state_id,
        [{"type": "click", "element_id": "e1"}],
    )
    assert result["state_consumed"] is True
    assert backend.actions[0][0] == {"fingerprint": "stable"}

    with pytest.raises(GuiStaleStateError, match="already consumed"):
        await gui.act(first, "window-1", state_id, [{"type": "wait"}])


@pytest.mark.asyncio
async def test_gui_invalid_action_does_not_consume_state(
    tmp_path: Path,
) -> None:
    gui, _backend, _store, first, _second = service(tmp_path)
    state_id = str(
        structured(await gui.snapshot(first, "window-1", screenshot=False))[
            "state_id"
        ]
    )

    with pytest.raises(ValueError, match="requires x and y"):
        await gui.act(first, "window-1", state_id, [{"type": "click"}])

    result = await gui.act(
        first, "window-1", state_id, [{"type": "wait", "seconds": 0}]
    )
    assert result["state_consumed"] is True


@pytest.mark.asyncio
async def test_gui_coordinate_action_rejects_geometry_change(
    tmp_path: Path,
) -> None:
    gui, backend, _store, first, _second = service(tmp_path)
    state_id = str(
        structured(await gui.snapshot(first, "window-1", screenshot=False))[
            "state_id"
        ]
    )
    backend.bounds["width"] = 41

    with pytest.raises(GuiStaleStateError, match="moved or resized"):
        await gui.act(
            first, "window-1", state_id, [{"type": "click", "x": 3, "y": 4}]
        )

    assert backend.actions == []


@pytest.mark.asyncio
async def test_gui_state_expires_and_owner_cleanup_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gui, _backend, _store, first, _second = service(tmp_path)
    now = {"value": 100.0}
    monkeypatch.setattr(gui_base.time, "monotonic", lambda: now["value"])
    expired = str(
        structured(await gui.snapshot(first, "window-1", screenshot=False))[
            "state_id"
        ]
    )
    live = str(
        structured(await gui.snapshot(first, "window-1", screenshot=False))[
            "state_id"
        ]
    )

    now["value"] = 131.0
    with pytest.raises(GuiStaleStateError, match="stale"):
        await gui.act(first, "window-1", expired, [{"type": "wait"}])

    now["value"] = 100.0
    newer = str(
        structured(await gui.snapshot(first, "window-1", screenshot=False))[
            "state_id"
        ]
    )
    assert await gui.discard_owner(first) == 1
    with pytest.raises(GuiStaleStateError, match="stale"):
        await gui.act(first, "window-1", newer, [{"type": "wait"}])
    del live


@pytest.mark.asyncio
async def test_session_end_discards_gui_observations(tmp_path: Path) -> None:
    gui, _backend, store, first, _second = service(tmp_path)
    state_id = str(
        structured(await gui.snapshot(first, "window-1", screenshot=False))[
            "state_id"
        ]
    )
    settings = Settings(
        default_workdir=tmp_path / "workspace",
        state_dir=tmp_path / "state",
        agent_bridge_enabled=False,
    )
    config = resolve_executor_config(settings)
    sessions = ExecutorSessionService(
        config,
        store,
        ShellService(config, store),
        gui=gui,
    )

    ended = await sessions.terminate(first)
    assert ended["absent"] is True
    assert state_id not in gui._states


@pytest.mark.asyncio
async def test_gui_service_close_clears_state_and_backend(
    tmp_path: Path,
) -> None:
    gui, backend, _store, first, _second = service(tmp_path)
    await gui.snapshot(first, "window-1", screenshot=False)

    await gui.aclose()

    assert gui._states == {}
    assert backend.closed is True
    with pytest.raises(RuntimeError, match="closed"):
        await gui.list_windows(first)


@pytest.mark.asyncio
async def test_gui_backend_is_owned_by_workgate_session(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        default_workdir=workspace,
        state_dir=tmp_path / "state",
        agent_bridge_enabled=False,
    )
    config = resolve_executor_config(settings)
    store = build_tool_session_store(settings)
    first = "sess_0000000000000000000001"
    second = "sess_0000000000000000000002"
    store.create_session(session_id=first, workdir=workspace)
    store.create_session(session_id=second, workdir=workspace)
    backends: list[FakeGuiBackend] = []

    def factory() -> FakeGuiBackend:
        backend = FakeGuiBackend()
        backends.append(backend)
        return backend

    gui = GuiService(config, store, backend_factory=factory)
    await asyncio.gather(gui.list_windows(first), gui.list_windows(second))

    assert len(backends) == 2
    assert gui._owner_backends[first] is not gui._owner_backends[second]

    await gui.discard_owner(first)
    assert backends[0].closed is True
    assert backends[1].closed is False
    assert first not in gui._owner_backends
    assert second in gui._owner_backends

    await gui.aclose()
    assert backends[1].closed is True


@pytest.mark.asyncio
async def test_gui_native_operations_are_serialized(tmp_path: Path) -> None:
    gui, backend, _store, first, _second = service(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = backend.list_windows

    async def held_list() -> dict[str, Any]:
        entered.set()
        await release.wait()
        return await original()

    backend.list_windows = held_list
    first_call = asyncio.create_task(gui.list_windows(first))
    await entered.wait()
    second_call = asyncio.create_task(gui.list_windows(first))
    await asyncio.sleep(0)
    assert not second_call.done()
    release.set()
    await asyncio.gather(first_call, second_call)


@pytest.mark.asyncio
async def test_gui_state_native_content_crosses_session_executor_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    harness = build_paired_control_harness(get_settings())
    backend = FakeGuiBackend()
    harness.executor.gui._backend = backend
    monkeypatch.setattr(
        "workgate.executor.hello.gui_capability_available",
        lambda: True,
    )
    mcp = build_mcp(runtime=harness.control)

    session_id = mcp_structured(
        await mcp.call_tool("session_start", {"workdir": "."})
    )["session_id"]
    listed = mcp_structured(
        await mcp.call_tool("gui_list", {"session_id": session_id})
    )
    assert listed["session_id"] == session_id
    assert listed["windows"][0]["id"] == "window-1"

    response = await mcp.call_tool(
        "gui_state",
        {
            "session_id": session_id,
            "window_id": "window-1",
        },
    )
    assert isinstance(response, CallToolResult)
    assert isinstance(response.content[0], ImageContent)
    assert response.structured_content is not None
    assert response.structured_content["session_id"] == session_id
    state_id = response.structured_content["state_id"]

    acted = mcp_structured(
        await mcp.call_tool(
            "gui_action",
            {
                "session_id": session_id,
                "window_id": "window-1",
                "state_id": state_id,
                "actions": [{"type": "click", "element_id": "e1"}],
            },
        )
    )
    assert acted["state_consumed"] is True
    assert backend.actions[-1][0] == {"fingerprint": "stable"}


@pytest.mark.asyncio
async def test_gui_service_close_waits_for_inflight_native_operation(
    tmp_path: Path,
) -> None:
    gui, backend, _store, first, _second = service(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = backend.list_windows

    async def held_list() -> dict[str, Any]:
        entered.set()
        await release.wait()
        return await original()

    backend.list_windows = held_list
    call = asyncio.create_task(gui.list_windows(first))
    await entered.wait()
    closing = asyncio.create_task(gui.aclose())
    await asyncio.sleep(0)
    assert not closing.done()
    assert backend.closed is False

    release.set()
    await call
    await closing
    assert backend.closed is True


def test_gui_payload_bounding_helpers_cover_edge_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert gui_base._truncate_gui_text("ok", 8) == "ok"
    assert gui_base._truncate_gui_text("abcdef", 4) == "a..."

    element = gui_base._bounded_element_record(
        {
            "id": "e1",
            "role": "button",
            "name": "Run",
            "automation_id": "run-button",
            "value": "value",
            "description": "description",
            "enabled": True,
            "focused": False,
            "editable": False,
            "offscreen": False,
            "depth": 2,
            "bounds": {"x": 1, "y": 2, "width": 3, "height": 4, "z": 9},
            "actions": [f"a{index}" for index in range(40)],
        }
    )
    assert element["bounds"] == {"x": 1, "y": 2, "width": 3, "height": 4}
    assert len(element["actions"]) == 32

    raw: list[Any] = [None, {"id": "e1"}, {"role": "label"}]
    bounded, ids = gui_base._bounded_elements(raw)
    assert len(bounded) == 2
    assert ids == {"e1"}

    monkeypatch.setattr(gui_base, "GUI_MAX_ELEMENTS_TOTAL_BYTES", 2)
    assert gui_base._bounded_elements([{"id": "e2"}]) == ([], set())

    assert (
        gui_base._bounded_window_record(
            {"id": "x" * (gui_base.GUI_MAX_WINDOW_ID_BYTES + 1)}
        )
        is None
    )
    window = gui_base._bounded_window_record(
        {
            "id": "w",
            "title": "Title",
            "app": "App",
            "pid": object(),
            "bounds": {"x": 1, "y": 2, "width": 3, "height": 4, "extra": 5},
        }
    )
    assert window is not None
    assert window["pid"] == 0
    assert window["bounds"] == {"x": 1, "y": 2, "width": 3, "height": 4}
    assert gui_base._bounded_window_records("not-a-list") == []
    assert gui_base._bounded_window_records([None, {"id": "w"}]) == [
        {"id": "w"}
    ]


def test_gui_screenshot_normalization_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "capture.png"
    Image.new("RGB", (4, 3), "white").save(capture, format="PNG")

    gui_base._normalize_screenshot_coordinates(capture, {})
    gui_base._normalize_screenshot_coordinates(
        capture, {"bounds": {"width": "bad", "height": 3}}
    )
    gui_base._normalize_screenshot_coordinates(
        capture, {"bounds": {"width": 0, "height": 3}}
    )

    gui_base._normalize_screenshot_coordinates(
        capture, {"bounds": {"width": 8, "height": 6}}
    )
    with Image.open(capture) as image:
        assert image.size == (8, 6)

    gui_base._normalize_screenshot_coordinates(
        capture, {"bounds": {"width": 8, "height": 6}}
    )

    with pytest.raises(GuiUnavailableError, match="window dimensions"):
        gui_base._normalize_screenshot_coordinates(
            capture,
            {
                "bounds": {
                    "width": gui_base.GUI_MAX_CAPTURE_DIMENSION + 1,
                    "height": 1,
                }
            },
        )

    monkeypatch.setattr(gui_base, "GUI_MAX_CAPTURE_DIMENSION", 4)
    with pytest.raises(GuiUnavailableError, match="Captured GUI image"):
        gui_base._normalize_screenshot_coordinates(
            capture, {"bounds": {"width": 4, "height": 4}}
        )


def test_gui_coordinate_and_freshness_helpers_cover_fail_closed_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert gui_base._bounds_tuple(None) is None
    assert gui_base._bounds_tuple({"x": 1}) is None
    assert gui_base._bounds_tuple(
        {"x": 1, "y": 2, "width": 10, "height": 8}
    ) == (1, 2, 10, 8)

    elements = [
        {"id": "e1", "bounds": {"x": 4, "y": 5, "width": 2, "height": 3}}
    ]
    assert gui_base._window_relative_elements(elements, {}) == elements
    assert gui_base._window_relative_elements(
        [*elements, {"id": "e2"}],
        {"bounds": {"x": 1, "y": 2, "width": 10, "height": 8}},
    ) == [
        {"id": "e1", "bounds": {"x": 3, "y": 3, "width": 2, "height": 3}},
        {"id": "e2"},
    ]

    window = {"bounds": {"x": 1, "y": 2, "width": 10, "height": 8}}
    gui_base._validate_window_relative_point(window, 0, 0, label="point")
    with pytest.raises(ValueError, match="invalid bounds"):
        gui_base._validate_window_relative_point({}, 0, 0, label="point")
    with pytest.raises(ValueError, match="integer"):
        gui_base._validate_window_relative_point(
            window, object(), 0, label="point"
        )
    with pytest.raises(ValueError, match="outside"):
        gui_base._validate_window_relative_point(window, 10, 0, label="point")

    with pytest.raises(ValueError, match="both x and y"):
        gui_base._validate_coordinate_action(
            window, {"type": "click", "x": 1}, has_locator=False
        )
    with pytest.raises(ValueError, match="requires x and y"):
        gui_base._validate_coordinate_action(
            window, {"type": "click"}, has_locator=False
        )
    gui_base._validate_coordinate_action(
        window, {"type": "click"}, has_locator=True
    )
    with pytest.raises(ValueError, match="drag requires"):
        gui_base._validate_coordinate_action(
            window,
            {"type": "drag", "x": 1, "y": 1},
            has_locator=False,
        )
    gui_base._validate_coordinate_action(
        window,
        {"type": "drag", "x": 1, "y": 1, "to_x": 2, "to_y": 3},
        has_locator=False,
    )

    assert gui_base.quantize_scroll_amount(0) == 0
    assert gui_base.quantize_scroll_amount(-0.2) == -1
    assert gui_base.quantize_scroll_amount(500) == 100

    gui_base._assert_action_fresh({})
    with pytest.raises(GuiStaleStateError, match="deadline is invalid"):
        gui_base._assert_action_fresh({"_observation_deadline": object()})
    monkeypatch.setattr(gui_base.time, "monotonic", lambda: 10.0)
    with pytest.raises(GuiStaleStateError, match="expired"):
        gui_base._assert_action_fresh({"_observation_deadline": 9.0})
