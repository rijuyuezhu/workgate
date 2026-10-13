"""Linux GUI combines desktop-native capabilities without replacing AT-SPI."""

from pathlib import Path

import pytest

from workgate.errors import GuiUnavailableError
from workgate.executor.gui import linux

WINDOW = {
    "id": "atspi:42:stable",
    "pid": 42,
    "title": "Demo",
    "bounds": {"x": 10, "y": 20, "width": 100, "height": 70},
}


@pytest.mark.parametrize(
    ("env", "capture", "activation", "raw_input", "available", "needs_focus"),
    [
        (
            {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":1"},
            "_capture_x11_desktop",
            "_activate_atspi",
            "_perform_x11",
            True,
            False,
        ),
        (
            {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "GNOME"},
            "_capture_portal_desktop",
            "_activate_atspi",
            "_perform_wayland",
            False,
            True,
        ),
        (
            {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "KDE"},
            "_capture_portal_desktop",
            "_activate_atspi",
            "_perform_wayland",
            False,
            True,
        ),
        (
            {
                "XDG_SESSION_TYPE": "wayland",
                "XDG_CURRENT_DESKTOP": "niri",
                "NIRI_SOCKET": "/run/user/1000/niri.sock",
            },
            "_capture_niri_desktop",
            "_activate_niri",
            "_perform_wayland",
            True,
            False,
        ),
        (
            {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "niri"},
            "_capture_niri_desktop",
            "_activate_niri",
            "_perform_wayland",
            False,
            False,
        ),
    ],
)
def test_desktop_operations_are_composed_independently(
    monkeypatch,
    env,
    capture,
    activation,
    raw_input,
    available,
    needs_focus,
):
    monkeypatch.setattr(
        linux.shutil,
        "which",
        lambda name: "/usr/bin/niri" if name == "niri" else None,
    )
    ops = linux.LinuxGuiBackend()._desktop_operations(env)
    assert ops.capture is getattr(linux, capture)
    assert getattr(ops.activate, "__func__", None) is getattr(
        linux.LinuxGuiBackend, activation
    )
    assert getattr(ops.input, "__func__", None) is getattr(
        linux.LinuxGuiBackend, raw_input
    )
    assert ops.capture_available is available
    assert ops.capture_requires_focus is needs_focus
    assert ops.input_devices is (
        linux._x11_input_devices
        if env["XDG_SESSION_TYPE"] == "x11"
        else linux.portal_available_devices
    )
    assert ops.input_transports == (
        (None, None)
        if env["XDG_SESSION_TYPE"] == "x11"
        else ("xdg-desktop-portal", "xdg-desktop-portal")
    )


def test_missing_desktop_transport_fails_closed():
    with pytest.raises(GuiUnavailableError, match="No graphical Linux session"):
        linux.LinuxGuiBackend()._desktop_operations({})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session", "capture_helper", "expected"),
    [
        ("x11", "_capture_x11", "x11-composite"),
        ("niri", "_capture_niri_window", "niri-ipc"),
        ("portal", "_capture_wayland", "xdg-desktop-portal"),
    ],
)
async def test_capture_calls_selected_existing_implementation(
    monkeypatch,
    tmp_path: Path,
    session,
    capture_helper,
    expected,
):
    received = []

    async def capture(*args):
        received.extend(args)
        return expected

    monkeypatch.setattr(linux, capture_helper, capture)
    backend = linux.LinuxGuiBackend()
    env = {"XDG_SESSION_TYPE": "x11" if session == "x11" else "wayland"}
    if session == "niri":
        env["XDG_CURRENT_DESKTOP"] = "niri"
    ops = backend._desktop_operations(env)
    filename = tmp_path / "frame.png"
    result = await ops.capture(filename, WINDOW, [], env)
    assert result == expected
    if session == "portal":
        assert received == [filename, WINDOW["bounds"], [], env]
    else:
        assert received == [filename, WINDOW, env]


@pytest.mark.asyncio
@pytest.mark.parametrize("session", ["x11", "portal", "niri"])
async def test_raw_input_delegates_to_chosen_implementation(
    monkeypatch, session
):
    backend = linux.LinuxGuiBackend()
    env = {"XDG_SESSION_TYPE": "x11" if session == "x11" else "wayland"}
    if session == "niri":
        env["XDG_CURRENT_DESKTOP"] = "niri"
    calls = []

    async def raw(window, locator, action, selected_env):
        calls.append((window, locator, action, selected_env))
        return {"path": "native"}

    monkeypatch.setattr(backend, "_perform_x11", raw)
    monkeypatch.setattr(backend, "_perform_wayland", raw)
    backend._env = env

    async def no_focus(*_args, **_kwargs):
        return None

    monkeypatch.setattr(backend, "_focus_window", no_focus)
    action = {"type": "move", "x": 3, "y": 4}
    result = await backend.perform_action(WINDOW, None, action)
    assert result == {"path": "native"}
    assert calls == [(WINDOW, None, action, env)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session", "expected_method", "focus_count"),
    [
        ("x11", "x11-composite", 0),
        ("portal", "xdg-desktop-portal", 1),
        ("niri", "niri-ipc", 0),
    ],
)
async def test_snapshot_preserves_focus_and_capture_semantics(
    monkeypatch,
    tmp_path,
    session,
    expected_method,
    focus_count,
):
    env = {"XDG_SESSION_TYPE": "x11" if session == "x11" else "wayland"}
    if session == "niri":
        env["XDG_CURRENT_DESKTOP"] = "niri"

    backend = linux.LinuxGuiBackend()
    backend._env = env
    focuses = []
    captures = []

    def helper(_payload, _env):
        return {"window": dict(WINDOW), "elements": [], "locators": {}}

    async def focus(*_args, **_kwargs):
        focuses.append(True)

    async def capture(*args):
        captures.append(args)
        return expected_method

    async def device_types(_env):
        return 0

    capture_fn = {
        "x11": "_capture_x11",
        "portal": "_capture_wayland",
        "niri": "_capture_niri_window",
    }[session]
    monkeypatch.setattr(linux, capture_fn, capture)
    monkeypatch.setattr(linux, "portal_available_devices", device_types)
    monkeypatch.setattr(backend, "_helper", helper)
    monkeypatch.setattr(backend, "_list_data", lambda _env: {"monitors": []})
    monkeypatch.setattr(backend, "_focus_window", focus)
    snapshot = await backend.snapshot(
        WINDOW["id"],
        screenshot_path=tmp_path / "image.png",
        include_elements=False,
        max_elements=1,
        max_depth=1,
    )
    assert snapshot.capabilities["capture_backend"] == expected_method
    assert len(focuses) == focus_count
    assert len(captures) == 1
    assert captures[0][1] == (
        WINDOW["bounds"] if session == "portal" else WINDOW
    )
