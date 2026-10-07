import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from workgate.errors import GuiUnavailableError
from workgate.executor.gui import linux, linux_atspi_helper, macos
from workgate.executor.gui.macos import MacOSGuiBackend


def test_linux_atspi_helper_supports_distro_python_310_grammar() -> None:
    helper = Path(linux_atspi_helper.__file__).read_text(encoding="utf-8")
    ast.parse(helper, feature_version=(3, 10))


def test_macos_semantic_target_point_is_fenced_to_window(monkeypatch) -> None:
    window = {"bounds": {"x": 100, "y": 100, "width": 200, "height": 100}}
    monkeypatch.setattr(macos, "_ax_bounds", lambda _ax, locator: locator)

    assert MacOSGuiBackend._screen_point(
        object(),
        window,
        {"type": "move"},
        {"x": 120, "y": 130, "width": 20, "height": 20},
    ) == (130, 140)

    with pytest.raises(ValueError, match="no usable"):
        MacOSGuiBackend._screen_point(
            object(),
            window,
            {"type": "scroll"},
            {"x": 0, "y": 0, "width": 0, "height": 0},
        )
    with pytest.raises(ValueError, match="outside"):
        MacOSGuiBackend._screen_point(
            object(),
            window,
            {"type": "drag"},
            {"x": 400, "y": 130, "width": 20, "height": 20},
        )


def test_linux_desktop_environment_preserves_explicit_process_values(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DISPLAY", ":5")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(
        linux.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )
    monkeypatch.setattr(
        linux.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                "DISPLAY=:0\n"
                "WAYLAND_DISPLAY=wayland-1\n"
                "XDG_SESSION_TYPE=wayland\n"
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\n"
            ),
        ),
    )

    env = linux._desktop_environment()

    assert env["DISPLAY"] == ":5"
    assert env["XDG_SESSION_TYPE"] == "x11"
    assert env["WAYLAND_DISPLAY"] == "wayland-1"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"


def test_linux_desktop_environment_uses_systemd_for_missing_values(
    monkeypatch,
) -> None:
    for name in (
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_SESSION_TYPE",
        "DBUS_SESSION_BUS_ADDRESS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        linux.shutil, "which", lambda _name: "/usr/bin/systemctl"
    )
    monkeypatch.setattr(
        linux.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                "DISPLAY=:0\n"
                "XDG_SESSION_TYPE=x11\n"
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus\n"
            ),
        ),
    )

    env = linux._desktop_environment()

    assert env["DISPLAY"] == ":0"
    assert env["XDG_SESSION_TYPE"] == "x11"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"


def test_linux_helper_timeout_releases_only_confirmed_held_inputs(
    monkeypatch,
) -> None:
    payloads: list[dict] = []
    monkeypatch.setattr(
        linux, "_helper_python", lambda _env: "/usr/bin/python3"
    )
    monkeypatch.setattr(linux, "_helper_path", lambda: Path("/tmp/helper.py"))

    def run(_argv, **kwargs):
        payload = json.loads(kwargs["input"])
        payloads.append(payload)
        if len(payloads) == 1:
            progress_fd = kwargs["pass_fds"][0]
            linux.os.write(
                progress_fd,
                (
                    b'{"kind":"key","symbol":1001,"state":"press"}\n'
                    b'{"kind":"key","symbol":1002,"state":"press"}\n'
                    b'{"kind":"key","symbol":1002,"state":"release"}\n'
                ),
            )
            raise linux.subprocess.TimeoutExpired(cmd="helper", timeout=30)
        return SimpleNamespace(stdout='{"ok": true}\n', stderr="", returncode=0)

    monkeypatch.setattr(linux.subprocess, "run", run)

    with pytest.raises(GuiUnavailableError, match="helper timed out"):
        linux._run_helper(
            {"command": "raw", "kind": "key_chord", "keys": ["CTRL", "A"]},
            {},
        )

    assert payloads[1] == {
        "command": "release_inputs",
        "pressed": [{"kind": "key", "symbol": 1001}],
    }


def test_atspi_timeout_cleanup_releases_confirmed_pressed_inputs() -> None:
    events: list[tuple] = []

    def mouse(x, y, event):
        events.append(("mouse", x, y, event))
        return True

    def key(symbol, text, synth_type):
        events.append(("key", symbol, text, synth_type))
        return True

    previous = linux_atspi_helper.Atspi
    linux_atspi_helper.Atspi = SimpleNamespace(
        KeySynthType=SimpleNamespace(RELEASE="release"),
        generate_mouse_event=mouse,
        generate_keyboard_event=key,
    )
    try:
        pointer = linux_atspi_helper._release_inputs(
            {
                "pressed": [
                    {"kind": "mouse", "button": 1, "x": 50, "y": 60},
                ]
            }
        )
        keyboard = linux_atspi_helper._release_inputs(
            {
                "pressed": [
                    {
                        "kind": "key",
                        "symbol": linux_atspi_helper._MODIFIERS["CTRL"],
                    },
                    {"kind": "key", "symbol": ord("a")},
                ]
            }
        )
    finally:
        linux_atspi_helper.Atspi = previous

    assert pointer == {"released": 1}
    assert keyboard == {"released": 2}
    assert events == [
        ("mouse", 50, 60, "b1r"),
        ("key", ord("a"), None, "release"),
        (
            "key",
            linux_atspi_helper._MODIFIERS["CTRL"],
            None,
            "release",
        ),
    ]


def test_linux_window_fingerprint_rejects_recycled_window(monkeypatch) -> None:
    replacement = object()

    class App:
        @staticmethod
        def get_process_id() -> int:
            return 4242

        @staticmethod
        def get_child_count() -> int:
            return 1

        @staticmethod
        def get_child_at_index(index: int):
            assert index == 0
            return replacement

    monkeypatch.setattr(linux_atspi_helper, "_apps", lambda: [App()])
    monkeypatch.setattr(
        linux_atspi_helper,
        "_window_signature",
        lambda window: "replacement" if window is replacement else None,
    )

    with pytest.raises(LookupError, match="no longer available"):
        linux_atspi_helper._resolve_window("atspi:4242:observed")


def test_linux_semantic_locator_rejects_recycled_element(monkeypatch) -> None:
    window = object()
    element = object()
    monkeypatch.setattr(
        linux_atspi_helper,
        "_resolve_window",
        lambda _window_id: (object(), window, 0),
    )
    monkeypatch.setattr(
        linux_atspi_helper,
        "_resolve_path",
        lambda candidate, path: (
            element if candidate is window and path == [1, 2] else None
        ),
    )
    monkeypatch.setattr(
        linux_atspi_helper, "_accessible_id", lambda _obj: "replacement"
    )
    monkeypatch.setattr(
        linux_atspi_helper, "_element_signature", lambda _obj: "new-fingerprint"
    )

    with pytest.raises(LookupError, match="changed since observation"):
        linux_atspi_helper._resolve_locator(
            {
                "window_id": "window-1",
                "locator": {
                    "path": [1, 2],
                    "accessible_id": "original",
                    "fingerprint": "old-fingerprint",
                },
            }
        )


@pytest.mark.asyncio
async def test_macos_semantic_locator_rejects_recycled_identity(
    monkeypatch,
) -> None:
    backend = MacOSGuiBackend()
    observed = object()
    replacement = object()
    fake_ax = SimpleNamespace(kAXChildrenAttribute="children")
    monkeypatch.setattr(macos, "_native", lambda: (fake_ax, object()))
    monkeypatch.setattr(backend, "_find_ax_window", lambda _record: object())
    monkeypatch.setattr(
        macos,
        "_ax_copy_values",
        lambda _ax, _element, _attribute, _index, _count: [replacement],
    )
    monkeypatch.setattr(
        macos, "_ax_element_fingerprint", lambda _ax, _element: "stable"
    )
    monkeypatch.setattr(
        macos, "_same_ax_element", lambda _ax, _left, _right: False
    )
    try:
        with pytest.raises(
            LookupError, match="identity changed since observation"
        ):
            backend._resolve_ax_locator(
                {"id": "cg:1"},
                {
                    "path": [0],
                    "fingerprint": "stable",
                    "_ax_element": observed,
                },
            )
    finally:
        await backend.aclose()


def test_macos_key_chord_releases_after_failed_key_up() -> None:
    posts: list[dict[str, object]] = []

    class Quartz:
        kCGEventFlagMaskShift = 1
        kCGEventFlagMaskControl = 2
        kCGEventFlagMaskAlternate = 4
        kCGEventFlagMaskCommand = 8
        kCGHIDEventTap = "tap"

        @staticmethod
        def CGEventCreateKeyboardEvent(_source, code, down):
            return {"code": code, "down": down, "flags": 0}

        @staticmethod
        def CGEventSetFlags(event, flags):
            event["flags"] = flags

        @staticmethod
        def CGEventPost(_tap, event):
            posts.append(dict(event))
            if len(posts) == 2:
                raise RuntimeError("injected key-up failure")

    with pytest.raises(RuntimeError, match="injected key-up failure"):
        MacOSGuiBackend._send_key_chord(Quartz, ["CTRL", "A"])

    assert [item["down"] for item in posts] == [True, False, False]


def test_linux_semantic_locator_rejects_recycled_element_identity(
    monkeypatch,
) -> None:
    window = object()
    element = object()
    monkeypatch.setattr(
        linux_atspi_helper,
        "_resolve_window",
        lambda _window_id: (object(), window, 0),
    )
    monkeypatch.setattr(
        linux_atspi_helper,
        "_resolve_path",
        lambda current_window, path: (
            element if current_window is window and path == [1, 2] else None
        ),
    )
    monkeypatch.setattr(
        linux_atspi_helper,
        "_accessible_id",
        lambda obj: "new-id" if obj is element else None,
    )
    monkeypatch.setattr(
        linux_atspi_helper,
        "_element_signature",
        lambda obj: "new-fingerprint" if obj is element else "",
    )

    with pytest.raises(LookupError, match="changed since observation"):
        linux_atspi_helper._resolve_locator(
            {
                "window_id": "window-1",
                "locator": {
                    "path": [1, 2],
                    "accessible_id": "old-id",
                    "fingerprint": "old-fingerprint",
                },
            }
        )


def test_macos_semantic_locator_rejects_recycled_ax_identity(
    monkeypatch,
) -> None:
    backend = MacOSGuiBackend()
    observed = object()
    current = object()
    window = object()
    fake_ax = SimpleNamespace(kAXChildrenAttribute="children")

    monkeypatch.setattr(macos, "_native", lambda: (fake_ax, object()))
    monkeypatch.setattr(backend, "_find_ax_window", lambda _record: window)
    monkeypatch.setattr(
        macos,
        "_ax_copy_values",
        lambda _ax, element, _attribute, _start, _count: (
            [current] if element is window else []
        ),
    )
    monkeypatch.setattr(
        macos,
        "_ax_element_fingerprint",
        lambda _ax, element: "stable-fingerprint",
    )
    monkeypatch.setattr(
        macos,
        "_same_ax_element",
        lambda _ax, left, right: left is right,
    )

    with pytest.raises(LookupError, match="identity changed"):
        backend._resolve_ax_locator(
            {},
            {
                "path": [0],
                "fingerprint": "stable-fingerprint",
                "_ax_element": observed,
            },
        )


def test_macos_key_cleanup_releases_after_partial_post_failure() -> None:
    posts: list[dict[str, object]] = []
    post_calls = 0

    def create_key_event(_source, code, down):
        return {"code": code, "down": down}

    def set_flags(event, flags):
        event["flags"] = flags

    def post(_tap, event):
        nonlocal post_calls
        post_calls += 1
        posts.append(dict(event))
        if post_calls == 2:
            raise RuntimeError("simulated key-up failure")

    quartz = SimpleNamespace(
        kCGEventFlagMaskShift=1,
        kCGEventFlagMaskControl=2,
        kCGEventFlagMaskAlternate=4,
        kCGEventFlagMaskCommand=8,
        kCGHIDEventTap="hid",
        CGEventCreateKeyboardEvent=create_key_event,
        CGEventSetFlags=set_flags,
        CGEventPost=post,
    )

    with pytest.raises(RuntimeError, match="simulated key-up failure"):
        MacOSGuiBackend._send_key_chord(quartz, ["CTRL", "A"])

    assert len(posts) == 3
    assert posts[0]["down"] is True
    assert posts[1]["down"] is False
    assert posts[2]["down"] is False
