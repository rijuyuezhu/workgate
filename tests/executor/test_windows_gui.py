"""Exercise the Windows UIA backend using isolated provider controls on Linux."""

import asyncio
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

import workgate.executor.gui as gui
from workgate.errors import GuiUnavailableError
from workgate.executor.gui import windows
from workgate.executor.gui.windows import WindowsGuiBackend


class Control:
    def __init__(
        self, *, handle=42, role="Window", name="Example", children=()
    ):
        self.NativeWindowHandle = handle
        self.ProcessId = 1234
        self.ClassName = "TestClass"
        self.AutomationId = "test-automation"
        self.ControlTypeName = role
        self.Name = name
        self.IsEnabled = True
        self.IsOffscreen = False
        self.BoundingRectangle = SimpleNamespace(
            left=20, top=30, right=220, bottom=130
        )
        self.children = list(children)
        self.next = None
        self.calls = []
        for left, right in zip(self.children, self.children[1:], strict=False):
            left.next = right

    def GetRuntimeId(self):
        return [self.NativeWindowHandle, 1]

    def GetFirstChildControl(self):
        return self.children[0] if self.children else None

    def GetNextSiblingControl(self):
        return self.next

    def SetFocus(self):
        self.calls.append("focus")

    def GetValuePattern(self):
        return SimpleNamespace(
            SetValue=lambda text: self.calls.append(("value", text))
        )

    def GetInvokePattern(self):
        return SimpleNamespace(Invoke=lambda: self.calls.append("invoke"))


@pytest.fixture
def provider(monkeypatch):
    child = Control(handle=0, role="Button", name="Apply")
    window = Control(children=(child,))
    root = Control(handle=0, children=(window,))
    events = []
    auto = SimpleNamespace(
        GetRootControl=lambda: root,
        ControlsAreSame=lambda x, y: x is y,
        Click=lambda x, y, **_kw: events.append(("click", x, y)),
        RightClick=lambda x, y, **_kw: events.append(("right_click", x, y)),
        MoveTo=lambda x, y, **_kw: events.append(("move", x, y)),
        SendKeys=lambda text, **_kw: events.append(("key", text)),
        DragDrop=lambda *values, **_kw: events.append(("drag", *values)),
        WheelDown=lambda amount, **_kw: events.append(("wheel_down", amount)),
        WheelUp=lambda amount, **_kw: events.append(("wheel_up", amount)),
    )
    bounds = {"x": 20, "y": 30, "width": 200, "height": 100}
    monkeypatch.setattr(windows, "_automation", lambda: auto)
    monkeypatch.setattr(windows, "_initialize_uia_thread", lambda: None)
    monkeypatch.setattr(
        windows, "_native_window_bounds", lambda _hwnd: dict(bounds)
    )
    monkeypatch.setattr(windows, "_is_iconic_window", lambda _hwnd: False)
    monkeypatch.setattr(
        windows,
        "_capture_window_image",
        lambda _hwnd, path: Image.new("RGB", (200, 100)).save(path),
    )
    return window, child, bounds, events


@pytest.mark.asyncio
async def test_windows_uia_lists_snapshots_and_captures(provider, tmp_path):
    window, child, _, _ = provider
    backend = WindowsGuiBackend()
    try:
        listing = await backend.list_windows()
        assert len(listing["windows"]) == 1
        window_id = listing["windows"][0]["id"]
        assert window_id.startswith("hwnd:42:")
        screenshot = tmp_path / "window.png"
        state = await backend.snapshot(
            window_id,
            screenshot_path=screenshot,
            include_elements=True,
            max_elements=5,
            max_depth=3,
        )
        assert screenshot.is_file()
        assert state.window["bounds"]["width"] == 200
        assert [item["name"] for item in state.elements] == ["Example", "Apply"]
        assert len(state.locators) == 2
        await backend.focus_window(state.window)
        assert "focus" in window.calls
        result = await backend.perform_action(
            state.window, state.locators["e2"], {"type": "click"}
        )
        assert result["method"] == "invoke"
        assert child.calls[-1] == "invoke"
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_windows_uia_coordinates_typing_scroll_and_drag(provider):
    _, _, _, events = provider
    backend = WindowsGuiBackend()
    try:
        window_id = (await backend.list_windows())["windows"][0]["id"]
        state = await backend.snapshot(
            window_id,
            screenshot_path=None,
            include_elements=False,
            max_elements=2,
            max_depth=1,
        )
        for action in (
            {"type": "click", "x": 10, "y": 10},
            {"type": "right_click", "x": 10, "y": 10},
            {"type": "move", "x": 15, "y": 20},
            {"type": "type", "text": "中文"},
            {"type": "key", "keys": "Ctrl+A"},
            {"type": "scroll", "x": 10, "y": 10, "delta_y": -2},
            {"type": "drag", "x": 10, "y": 10, "to_x": 50, "to_y": 40},
        ):
            await backend.perform_action(state.window, None, action)
        assert ("click", 30, 40) in events
        assert ("right_click", 30, 40) in events
        assert ("key", "中文") in events
        assert ("key", "{Ctrl}A") in events
        assert ("wheel_down", 2) in events
        assert ("drag", 30, 40, 70, 70) in events
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_windows_uia_rejects_stale_window_geometry_and_element(provider):
    window, child, bounds, events = provider
    backend = WindowsGuiBackend()
    try:
        window_id = (await backend.list_windows())["windows"][0]["id"]
        state = await backend.snapshot(
            window_id,
            screenshot_path=None,
            include_elements=True,
            max_elements=3,
            max_depth=2,
        )
        bounds["x"] += 5
        with pytest.raises(LookupError, match="moved or resized"):
            await backend.perform_action(
                state.window, None, {"type": "click", "x": 10, "y": 10}
            )
        assert not events
        bounds["x"] -= 5
        child.Name = "Replaced"
        with pytest.raises(LookupError, match="changed"):
            await backend.perform_action(
                state.window,
                state.locators["e2"],
                {"type": "set_value", "text": "a"},
            )
        window.NativeWindowHandle = 43
        with pytest.raises(LookupError, match="no longer available"):
            await backend.focus_window(state.window)
    finally:
        await backend.aclose()


def test_windows_gui_capability_headless_and_uninstalled(monkeypatch):
    monkeypatch.setattr(gui.platform, "system", lambda: "Windows")
    monkeypatch.setattr(gui.sys, "platform", "linux")
    assert gui.gui_capability_available() is False
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(gui.importlib.util, "find_spec", lambda _name: None)
    assert gui.gui_capability_available() is False
    monkeypatch.setattr(gui.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(windows, "windows_desktop_available", lambda: True)
    assert gui.gui_capability_available() is True


def test_windows_gui_errors_explicitly_on_missing_ui_automation(monkeypatch):
    monkeypatch.setitem(sys.modules, "uiautomation", None)
    with pytest.raises(GuiUnavailableError, match="uiautomation"):
        windows._automation()


def test_windows_gui_factory(monkeypatch):
    monkeypatch.setattr(gui.platform, "system", lambda: "Windows")
    backend = gui._backend_for_platform()
    assert backend.name == "windows-uia"
    asyncio.run(backend.aclose())
