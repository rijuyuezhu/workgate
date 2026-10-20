"""Exercise Win32/GDI capture and explicit failure bounds using fake native calls."""

import ctypes
from types import SimpleNamespace

import pytest
from PIL import Image

from workgate.errors import GuiUnavailableError
from workgate.executor.gui import windows_capture as capture


class NativeCall:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result(*args) if callable(self.result) else self.result


@pytest.fixture
def gdi(monkeypatch):
    state = {"width": 10, "height": 5, "print": True}

    def get_rect(_hwnd, ptr):
        rect = ptr._obj
        rect.left, rect.top = -100, 20
        rect.right = rect.left + state["width"]
        rect.bottom = rect.top + state["height"]
        return 1

    def pixels(_dc, _bitmap, _start, height, buffer, _info, _mode):
        ctypes.memset(buffer, 64, state["width"] * state["height"] * 4)
        return height

    user = SimpleNamespace(
        GetWindowRect=NativeCall(get_rect),
        IsIconic=NativeCall(0),
        GetWindowDC=NativeCall(11),
        ReleaseDC=NativeCall(1),
        PrintWindow=NativeCall(lambda *_args: state["print"]),
    )
    gdi32 = SimpleNamespace(
        CreateCompatibleDC=NativeCall(22),
        CreateCompatibleBitmap=NativeCall(33),
        SelectObject=NativeCall(44),
        GetDIBits=NativeCall(pixels),
        DeleteObject=NativeCall(1),
        DeleteDC=NativeCall(1),
    )
    monkeypatch.setattr(
        capture.ctypes,
        "windll",
        SimpleNamespace(user32=user, gdi32=gdi32),
        raising=False,
    )
    return state, user, gdi32


def test_win32_capture_only_selected_window(gdi, tmp_path):
    _, user, gdi32 = gdi
    assert capture._native_window_bounds(77) == {
        "x": -100,
        "y": 20,
        "width": 10,
        "height": 5,
    }
    output = tmp_path / "window.png"
    capture._capture_window_image_native(77, output)
    with Image.open(output) as image:
        assert image.size == (10, 5)
        assert image.mode == "RGB"
    assert len(user.PrintWindow.calls) == 1
    assert len(user.ReleaseDC.calls) == 1
    assert len(gdi32.DeleteDC.calls) == 1
    assert len(gdi32.DeleteObject.calls) == 1


@pytest.mark.parametrize(
    "target,value,match",
    [
        ("width", 0, "invalid capture bounds"),
        ("height", 0, "invalid capture bounds"),
        ("width", 20000, "safe budget"),
        ("height", 20000, "safe budget"),
    ],
)
def test_win32_capture_rejects_unbounded_geometry(
    gdi, tmp_path, target, value, match
):
    state, user, _gdi32 = gdi
    state[target] = value
    with pytest.raises(GuiUnavailableError, match=match):
        capture._capture_window_image_native(77, tmp_path / "unsafe.png")
    assert not user.GetWindowDC.calls


def test_win32_capture_rejects_minimized_and_no_dc(gdi, tmp_path):
    _, user, _ = gdi
    user.IsIconic.result = 1
    with pytest.raises(GuiUnavailableError, match="minimized"):
        capture._capture_window_image_native(77, tmp_path / "unsafe.png")
    user.IsIconic.result = 0
    user.GetWindowDC.result = 0
    with pytest.raises(GuiUnavailableError, match="window DC"):
        capture._capture_window_image_native(77, tmp_path / "unsafe.png")


def test_win32_capture_rejects_failed_print_and_releases_resources(
    gdi, tmp_path
):
    state, user, gdi32 = gdi
    state["print"] = False
    with pytest.raises(GuiUnavailableError, match="PrintWindow"):
        capture._capture_window_image_native(77, tmp_path / "unsafe.png")
    assert len(user.PrintWindow.calls) == 2
    assert len(user.ReleaseDC.calls) == 1
    assert len(gdi32.DeleteDC.calls) == 1


@pytest.mark.parametrize(
    "operation",
    ["CreateCompatibleDC", "CreateCompatibleBitmap", "SelectObject"],
)
def test_win32_capture_refuses_allocation_failure(gdi, tmp_path, operation):
    _, user, gdi32 = gdi
    getattr(gdi32, operation).result = 0
    with pytest.raises(GuiUnavailableError, match="off-screen window capture"):
        capture._capture_window_image_native(77, tmp_path / "unsafe.png")
    assert len(user.ReleaseDC.calls) == 1


def test_win32_capture_refuses_failed_readback(gdi, tmp_path):
    _, _, gdi32 = gdi
    gdi32.GetDIBits.result = 0
    with pytest.raises(GuiUnavailableError, match="captured window bitmap"):
        capture._capture_window_image_native(77, tmp_path / "unsafe.png")


def test_win32_capture_checks_missing_native_bindings(monkeypatch, tmp_path):
    monkeypatch.delattr(capture.ctypes, "windll", raising=False)
    with pytest.raises(GuiUnavailableError, match="unavailable"):
        capture._native_window_bounds(33)
    with pytest.raises(GuiUnavailableError, match="unavailable"):
        capture._capture_window_image_native(33, tmp_path / "unsafe.png")


def test_capture_cli_rejects_other_platforms_and_bad_handles(monkeypatch):
    monkeypatch.setattr(capture.sys, "platform", "linux")
    monkeypatch.setattr(capture.sys, "argv", ["capture", "1", "window.png"])
    with pytest.raises(SystemExit, match="usage"):
        capture.main()
    monkeypatch.setattr(capture.sys, "platform", "win32")
    monkeypatch.setattr(capture.sys, "argv", ["capture", "0", "window.png"])
    with pytest.raises(SystemExit, match="invalid window handle"):
        capture.main()


def test_windows_frozen_capture_entrypoint_args(monkeypatch, tmp_path):
    output = tmp_path / "window.png"
    called = []
    monkeypatch.setattr(capture.sys, "platform", "win32")
    monkeypatch.setattr(capture, "_set_thread_dpi_awareness", lambda: None)
    monkeypatch.setattr(
        capture,
        "_capture_window_image_native",
        lambda hwnd, destination: called.append((hwnd, destination)),
    )
    capture.main(["42", str(output)])
    assert called == [(42, output)]
    with pytest.raises(SystemExit, match="invalid window handle"):
        capture.main(["0", str(output)])
