"""Windows UIA identity, action, capture and watchdog regression branches."""

import subprocess
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.executor.test_windows_gui import Control
from tests.executor.test_windows_gui import provider as fake_provider
from workgate.errors import GuiUnavailableError
from workgate.executor.gui import windows
from workgate.executor.gui.windows import WindowsGuiBackend


@pytest.fixture
def provider(monkeypatch):
    return cast(Any, fake_provider).__wrapped__(monkeypatch)


@pytest.mark.parametrize(
    "keys,result",
    [
        ("ctrl+shift+F4", "{Ctrl}{Shift}{F4}"),
        (["Meta", "Left"], "{Win}{Left}"),
        ("Alt+Enter", "{Alt}{Enter}"),
        ("PageDown", "{PageDown}"),
        ("a", "a"),
    ],
)
def test_windows_key_sequence_supported(keys, result):
    assert windows._key_sequence(keys) == result


@pytest.mark.parametrize("keys", ["", None, [], 8, "Alt+Nonsense"])
def test_windows_key_sequence_rejects_unknown_keys(keys):
    with pytest.raises(ValueError):
        windows._key_sequence(keys)


def test_windows_uia_invalid_geometry_property_and_runtime_identity(
    monkeypatch,
):
    assert windows._rect_dict(None)["width"] == 0
    assert windows._rect_dict(SimpleNamespace(left=4))["width"] == 0

    class Broken:
        @property
        def Name(self):
            raise RuntimeError("inaccessible")

        def GetRuntimeId(self):
            raise RuntimeError("inaccessible")

    assert windows._safe_property(Broken(), "Name", "fallback") == "fallback"
    assert windows._control_runtime_id(Broken()) == ""
    control = Control()
    monkeypatch.setattr(control, "GetRuntimeId", lambda: list(range(1025)))
    with pytest.raises(ValueError, match="runtime ID"):
        windows._control_runtime_id(control)
    monkeypatch.setattr(control, "GetRuntimeId", lambda: "x" * 2000)
    with pytest.raises(ValueError, match="runtime ID"):
        windows._control_runtime_id(control)


def test_windows_uia_control_identity_fails_closed(monkeypatch):
    monkeypatch.setattr(
        windows,
        "_automation",
        lambda: SimpleNamespace(ControlsAreSame=lambda a, b: False),
    )
    assert not windows._same_uia_control(object(), object())
    assert not windows._same_uia_control(None, object())
    monkeypatch.setattr(windows, "_automation", lambda: SimpleNamespace())
    assert not windows._same_uia_control(object(), object())
    monkeypatch.setattr(
        windows,
        "_automation",
        lambda: SimpleNamespace(ControlsAreSame=lambda a, b: 1 / 0),
    )
    assert not windows._same_uia_control(object(), object())


def test_windows_uia_window_filters_and_bounded_traversal(provider):
    window, _, _, _ = provider
    assert windows._window_record(Control(handle=0)) is None
    window.IsOffscreen = True
    assert windows._window_record(window) is None
    window.IsOffscreen = False
    record = windows._window_record(window)
    assert record is not None and record["bounds"]["width"] == 200
    empty = Control(children=[])
    assert list(windows._iter_control_children(empty, 0)) == []
    assert list(windows._iter_control_children(empty, 20)) == []


@pytest.mark.asyncio
async def test_windows_uia_rejects_invalid_window_or_locator(provider):
    window, child, _, _ = provider
    backend = WindowsGuiBackend()
    try:
        with pytest.raises(ValueError, match="Invalid Windows window"):
            await backend.snapshot(
                "not-a-window",
                screenshot_path=None,
                include_elements=True,
                max_elements=5,
                max_depth=3,
            )
        original = (await backend.list_windows())["windows"][0]["id"]
        snap = await backend.snapshot(
            original,
            screenshot_path=None,
            include_elements=True,
            max_elements=5,
            max_depth=3,
        )
        with pytest.raises(LookupError, match="invalid or incomplete"):
            await backend.perform_action(
                snap.window, {"path": []}, {"type": "focus"}
            )
        with pytest.raises(LookupError, match="invalid"):
            await backend.perform_action(
                snap.window, "not-a-locator", {"type": "focus"}
            )
        with pytest.raises(LookupError, match="no longer available"):
            await backend.perform_action(
                snap.window,
                {**snap.locators["e2"], "path": [999]},
                {"type": "focus"},
            )
        # A reused HWND with different UIA identity is rejected.
        window.AutomationId = "other"
        with pytest.raises(LookupError, match="identity changed"):
            await backend.focus_window(snap.window)
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_windows_uia_semantic_actions_and_fallback_control(
    provider, monkeypatch
):
    window, child, _, events = provider
    backend = WindowsGuiBackend()
    try:
        ident = (await backend.list_windows())["windows"][0]["id"]
        state = await backend.snapshot(
            ident,
            screenshot_path=None,
            include_elements=True,
            max_elements=5,
            max_depth=4,
        )
        locator = state.locators["e2"]
        assert (
            await backend.perform_action(
                state.window,
                locator,
                {
                    "type": "set_value",
                    "text": "ab中文",
                },
            )
        )["semantic"]
        assert ("value", "ab中文") in child.calls
        assert (
            await backend.perform_action(
                state.window, locator, {"type": "focus"}
            )
        )["semantic"]
        assert (
            await backend.perform_action(state.window, None, {"type": "focus"})
        )["semantic"]
        assert "focus" in child.calls and "focus" in window.calls
        with pytest.raises(ValueError, match="requires element_id"):
            await backend.perform_action(
                state.window, None, {"type": "set_value", "text": "a"}
            )
        monkeypatch.setattr(child, "GetValuePattern", lambda: None)
        with pytest.raises(ValueError, match="Value pattern"):
            await backend.perform_action(
                state.window, locator, {"type": "set_value", "text": "a"}
            )
        monkeypatch.setattr(child, "GetInvokePattern", lambda: None)
        monkeypatch.setattr(
            child,
            "Click",
            lambda **_kw: events.append("semantic-click"),
            raising=False,
        )
        monkeypatch.setattr(
            child,
            "DoubleClick",
            lambda **_kw: events.append("semantic-double"),
            raising=False,
        )
        monkeypatch.setattr(
            child,
            "RightClick",
            lambda **_kw: events.append("semantic-right"),
            raising=False,
        )
        for kind in ("click", "double_click", "right_click"):
            result = await backend.perform_action(
                state.window, locator, {"type": kind}
            )
            assert result["method"] == "control"
        assert events[-3:] == [
            "semantic-click",
            "semantic-double",
            "semantic-right",
        ]
        await backend.perform_action(
            state.window, locator, {"type": "type", "text": "abc"}
        )
        await backend.perform_action(
            state.window, locator, {"type": "key", "keys": "Enter"}
        )
        assert ("key", "abc") in events
        assert ("key", "{Enter}") in events
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_windows_uia_double_click_positive_scroll_and_error_cases(
    provider, monkeypatch
):
    window, child, _, events = provider
    backend = WindowsGuiBackend()
    monkeypatch.setattr(
        windows,
        "_horizontal_wheel",
        lambda delta: events.append(("horizontal", delta)),
    )
    try:
        window_id = (await backend.list_windows())["windows"][0]["id"]
        state = await backend.snapshot(
            window_id,
            screenshot_path=None,
            include_elements=True,
            max_elements=5,
            max_depth=3,
        )
        await backend.perform_action(
            state.window, None, {"type": "double_click", "x": 5, "y": 8}
        )
        await backend.perform_action(
            state.window,
            None,
            {
                "type": "scroll",
                "x": 5,
                "y": 8,
                "delta_y": 2,
                "delta_x": -1,
            },
        )
        assert events.count(("click", 25, 38)) == 2
        assert ("wheel_up", 2) in events
        assert ("horizontal", 1) in events
        assert (
            await backend.perform_action(
                state.window, None, {"type": "wait", "seconds": -1}
            )
        )["waited_s"] == 0
        with pytest.raises(ValueError, match="Unsupported GUI action"):
            await backend.perform_action(state.window, None, {"type": "bogus"})
        with pytest.raises(ValueError, match="requires x and y"):
            await backend.perform_action(state.window, None, {"type": "move"})
        child.BoundingRectangle = SimpleNamespace(
            left=0, top=0, right=0, bottom=0
        )
        with pytest.raises(ValueError, match="no usable screen bounds"):
            backend._screen_point(state.window, {}, child)
        child.BoundingRectangle = SimpleNamespace(
            left=0, top=0, right=5, bottom=5
        )
        with pytest.raises(ValueError, match="outside"):
            backend._screen_point(state.window, {}, child)
    finally:
        await backend.aclose()


def test_windows_gui_capture_child_process_success_failure_timeout(
    monkeypatch, tmp_path
):
    destination = tmp_path / "window.png"
    called = []

    def run(argv, **kwargs):
        called.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(windows.subprocess, "run", run)
    windows._capture_window_image(77, destination)
    assert called[-1][0][3:] == ["77", str(destination)]
    assert called[-1][1]["timeout"] == 15
    monkeypatch.setattr(
        windows.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, "", "bad HWND"
        ),
    )
    with pytest.raises(GuiUnavailableError, match="bad HWND"):
        windows._capture_window_image(77, destination)

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 15)

    monkeypatch.setattr(windows.subprocess, "run", timeout)
    with pytest.raises(GuiUnavailableError, match="timed out"):
        windows._capture_window_image(77, destination)
    monkeypatch.setattr(windows.sys, "frozen", True, raising=False)
    with pytest.raises(GuiUnavailableError, match="Frozen Windows"):
        windows._capture_window_image(77, destination)


@pytest.mark.asyncio
async def test_windows_uia_screenshot_detects_moving_or_missing_window(
    provider, tmp_path, monkeypatch
):
    window, child, bounds, events = provider
    backend = WindowsGuiBackend()
    try:
        wid = (await backend.list_windows())["windows"][0]["id"]

        def mutate(_hwnd, path):
            from PIL import Image

            Image.new("RGB", (200, 100)).save(path)
            bounds["width"] = 250

        monkeypatch.setattr(windows, "_capture_window_image", mutate)
        with pytest.raises(LookupError, match="moved or resized"):
            await backend.snapshot(
                wid,
                screenshot_path=tmp_path / "a.png",
                include_elements=False,
                max_elements=1,
                max_depth=1,
            )
        bounds["width"] = 200
        monkeypatch.setattr(
            windows, "_capture_window_image", lambda _hwnd, _path: None
        )
        with pytest.raises(GuiUnavailableError, match="did not produce"):
            await backend.snapshot(
                wid,
                screenshot_path=tmp_path / "b.png",
                include_elements=False,
                max_elements=1,
                max_depth=1,
            )
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_windows_uia_provider_watchdog_times_out(provider, monkeypatch):
    backend = WindowsGuiBackend()
    monkeypatch.setattr(windows, "_UIA_OPERATION_TIMEOUT_S", 0.01)
    try:
        with pytest.raises(GuiUnavailableError, match="timed out"):
            await backend._run_uia(time.sleep, 0.08)
    finally:
        await backend.aclose()
