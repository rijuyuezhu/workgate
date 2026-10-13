"""Default-off Niri keyboard uses the user session's virtual-keyboard protocol."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.errors import GuiStaleStateError, GuiUnavailableError
from workgate.executor.gui import linux

WINDOW = {
    "id": "atspi:47:fingerprint",
    "pid": 47,
    "title": "Target",
    "bounds": {"x": 0, "y": 0, "width": 250, "height": 90},
}
ENV = {
    "XDG_SESSION_TYPE": "wayland",
    "XDG_CURRENT_DESKTOP": "niri",
    "NIRI_SOCKET": "/run/user/1000/niri.sock",
    "WAYLAND_DISPLAY": "wayland-1",
}


def test_keyboard_opt_in_is_off_by_default(monkeypatch):
    monkeypatch.delenv(
        "WORKGATE_GUI_NIRI_NATIVE_KEYBOARD_ENABLED", raising=False
    )
    assert Settings().gui_niri_native_keyboard_enabled is False
    assert not resolve_executor_config(
        Settings()
    ).gui_niri_native_keyboard_enabled
    assert resolve_executor_config(
        Settings(gui_niri_native_keyboard_enabled=True)
    ).gui_niri_native_keyboard_enabled


@pytest.mark.asyncio
async def test_keyboard_composition_is_explicit_and_independent_of_pointer(
    monkeypatch,
):
    async def no_portal_devices(_env):
        return 0

    monkeypatch.setattr(linux, "portal_available_devices", no_portal_devices)
    monkeypatch.setattr(
        linux.shutil,
        "which",
        lambda binary: "/usr/bin/wtype" if binary == "wtype" else None,
    )
    disabled = linux.LinuxGuiBackend()
    enabled = linux.LinuxGuiBackend(niri_native_keyboard_enabled=True)
    disabled._env = enabled._env = dict(ENV)
    monkeypatch.setattr(
        disabled, "_list_data", lambda _env: {"windows": [WINDOW]}
    )
    monkeypatch.setattr(
        enabled, "_list_data", lambda _env: {"windows": [WINDOW]}
    )
    old = await disabled.list_windows()
    new = await enabled.list_windows()
    assert old["capabilities"]["keyboard_input"] is False
    assert new["capabilities"]["keyboard_input"] is True
    assert new["capabilities"]["coordinate_input"] is False
    assert new["capabilities"]["wayland_input"] == "niri-virtual-keyboard"
    assert (
        getattr(enabled._desktop_operations(ENV).input, "__func__", None)
        is linux.LinuxGuiBackend._perform_niri_input
    )
    assert (
        getattr(disabled._desktop_operations(ENV).input, "__func__", None)
        is linux.LinuxGuiBackend._perform_wayland
    )


@pytest.mark.asyncio
async def test_native_keyboard_disabled_when_binary_or_socket_missing(
    monkeypatch,
):
    async def no_portal_devices(_env):
        return 0

    monkeypatch.setattr(linux, "portal_available_devices", no_portal_devices)
    monkeypatch.setattr(linux.shutil, "which", lambda _: None)
    backend = linux.LinuxGuiBackend(niri_native_keyboard_enabled=True)
    backend._env = dict(ENV)
    monkeypatch.setattr(backend, "_list_data", lambda _: {"windows": [WINDOW]})
    assert (await backend.list_windows())["capabilities"][
        "keyboard_input"
    ] is False
    with pytest.raises(GuiUnavailableError, match="requires wtype"):
        await backend.perform_action(
            WINDOW, None, {"type": "key", "keys": "Return"}
        )


@pytest.mark.parametrize(
    ("keys", "argv"),
    [
        ("Return", ["-k", "Return"]),
        ("Ctrl+A", ["-M", "ctrl", "-k", "a", "-m", "ctrl"]),
        (
            ["SHIFT", "CTRL", "z"],
            [
                "-M",
                "shift",
                "-M",
                "ctrl",
                "-k",
                "z",
                "-m",
                "ctrl",
                "-m",
                "shift",
            ],
        ),
        ("Escape", ["-k", "Escape"]),
    ],
)
def test_native_key_chord_arguments(keys, argv):
    assert linux._niri_wtype_chord(keys) == argv


@pytest.mark.parametrize(
    "keys", ["Ctrl", "Ctrl+A+B", "Ctrl+Ctrl+A", "Unknown_Native_Key"]
)
def test_native_invalid_chords_rejected_before_input(keys):
    with pytest.raises(ValueError):
        linux._niri_wtype_chord(keys)


@pytest.mark.asyncio
async def test_native_keyboard_types_only_into_verified_focused_window(
    monkeypatch,
):
    backend = linux.LinuxGuiBackend(niri_native_keyboard_enabled=True)
    backend._env = dict(ENV)
    calls = []

    async def no_portal_devices(_env):
        return 0

    async def focused(window, _env):
        assert window is WINDOW
        calls.append("focused")

    async def keyboard(args, env, *, text=None):
        assert env is backend._env
        calls.append((tuple(args), text))

    monkeypatch.setattr(linux, "portal_available_devices", no_portal_devices)
    monkeypatch.setattr(
        linux.shutil,
        "which",
        lambda name: "/usr/bin/wtype" if name == "wtype" else None,
    )
    monkeypatch.setattr(linux, "_assert_niri_window_focused", focused)
    monkeypatch.setattr(linux, "_run_niri_wtype", keyboard)
    result = await backend._perform_niri_input(
        WINDOW, None, {"type": "type", "text": "hello"}, backend._env
    )
    assert result == {"characters": 5, "method": "niri-virtual-keyboard"}
    assert calls == ["focused", (("-d", "18", "-"), "hello"), "focused"]
    result = await backend._perform_niri_input(
        WINDOW, None, {"type": "key", "keys": "Return"}, backend._env
    )
    assert result["method"] == "niri-virtual-keyboard"
    assert calls[-3:] == ["focused", (("-k", "Return"), None), "focused"]


@pytest.mark.asyncio
async def test_native_keyboard_aborts_before_typing_when_focus_stolen(
    monkeypatch,
):
    backend = linux.LinuxGuiBackend(niri_native_keyboard_enabled=True)
    backend._env = dict(ENV)

    async def no_portal_devices(_env):
        return 0

    async def stolen(_window, _env):
        raise GuiStaleStateError("different window")

    async def cannot_type(*_):
        pytest.fail("Must not type in another focused window")

    monkeypatch.setattr(linux, "portal_available_devices", no_portal_devices)
    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wtype")
    monkeypatch.setattr(linux, "_assert_niri_window_focused", stolen)
    monkeypatch.setattr(linux, "_run_niri_wtype", cannot_type)
    with pytest.raises(GuiStaleStateError):
        await backend._perform_niri_input(
            WINDOW, None, {"type": "key", "keys": "Return"}, backend._env
        )


@pytest.mark.asyncio
async def test_native_keyboard_wtype_cancel_kills_process(monkeypatch):
    class SleepingProcess:
        returncode = None
        killed = False

        async def wait(self):
            if self.killed:
                return self.returncode
            await asyncio.sleep(3600)

        async def communicate(self, data=None):
            return await self.wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

    process = SleepingProcess()

    async def spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wtype")
    monkeypatch.setattr(linux.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(linux._run_niri_wtype(["-k", "a"], dict(ENV)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed


@pytest.mark.asyncio
async def test_niri_focus_verification_checks_real_compositor_id(monkeypatch):
    def run(argv, **_kwargs):
        if argv[-1] == "windows":
            return SimpleNamespace(
                stdout=json.dumps([{"id": 88, "pid": 47, "title": "Target"}])
            )
        if argv[-1] == "focused-window":
            return SimpleNamespace(
                stdout=json.dumps({"id": 89, "pid": 47, "title": "Target"})
            )
        raise AssertionError(argv)

    monkeypatch.setattr(linux.subprocess, "run", run)
    with pytest.raises(GuiStaleStateError, match="different window"):
        await linux._assert_niri_window_focused(WINDOW, dict(ENV))


@pytest.mark.asyncio
async def test_native_keyboard_timeout_kills_virtual_keyboard_process(
    monkeypatch,
):
    class Stalled:
        returncode = None
        killed = False

        async def wait(self):
            if self.killed:
                return -9
            await asyncio.sleep(3600)

        async def communicate(self, data=None):
            return await self.wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

    process = Stalled()

    async def spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wtype")
    monkeypatch.setattr(linux.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(linux, "_NIRI_WTYPE_TIMEOUT_S", 0.02)
    with pytest.raises(GuiUnavailableError, match="timed out"):
        await linux._run_niri_wtype(["-k", "a"], dict(ENV))
    assert process.killed


@pytest.mark.asyncio
@pytest.mark.parametrize("desktop", ["GNOME", "KDE"])
async def test_opt_in_does_not_replace_other_wayland_portal(
    monkeypatch, desktop
):
    backend = linux.LinuxGuiBackend(niri_native_keyboard_enabled=True)
    env = {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": desktop}
    ops = backend._desktop_operations(env)
    assert (
        getattr(ops.input, "__func__", None)
        is linux.LinuxGuiBackend._perform_wayland
    )
    assert ops.input_devices is linux.portal_available_devices


@pytest.mark.asyncio
async def test_opt_in_does_not_replace_niri_pointer_portal(monkeypatch):
    backend = linux.LinuxGuiBackend(niri_native_keyboard_enabled=True)
    env = dict(ENV)
    expected = {"method": "xdg-desktop-portal"}

    async def portal_pointer(window, locator, action, selected_env):
        assert window is WINDOW
        assert action["type"] == "move"
        assert selected_env == env
        return expected

    monkeypatch.setattr(backend, "_perform_wayland", portal_pointer)
    result = await backend._perform_niri_input(
        WINDOW, None, {"type": "move", "x": 2, "y": 3}, env
    )
    assert result is expected


@pytest.mark.asyncio
async def test_native_keyboard_rejects_long_text_before_running_client(
    monkeypatch,
):
    backend = linux.LinuxGuiBackend(niri_native_keyboard_enabled=True)
    backend._env = dict(ENV)
    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wtype")

    async def focused(*_args):
        return None

    async def must_not_send(*_args):
        pytest.fail("Long input must not spawn native keyboard")

    monkeypatch.setattr(linux, "_assert_niri_window_focused", focused)
    monkeypatch.setattr(linux, "_run_niri_wtype", must_not_send)
    with pytest.raises(ValueError, match="limited to 512"):
        await backend._perform_niri_input(
            WINDOW, None, {"type": "type", "text": "a" * 513}, backend._env
        )


@pytest.mark.asyncio
async def test_native_keyboard_sends_utf8_text_only_via_stdin(monkeypatch):
    received = []

    class Process:
        returncode = 0

        async def communicate(self, data=None):
            received.append(data)
            return (b"", b"")

    async def spawn(*args, **kwargs):
        assert "password-中文" not in args
        assert args[1:] == ("-d", "18", "-")
        assert kwargs["stdin"] == asyncio.subprocess.PIPE
        return Process()

    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wtype")
    monkeypatch.setattr(linux.asyncio, "create_subprocess_exec", spawn)
    await linux._run_niri_wtype(
        ["-d", "18", "-"], dict(ENV), text="password-中文"
    )
    assert received == ["password-中文".encode()]


@pytest.mark.asyncio
async def test_native_keyboard_reap_stall_is_bounded(monkeypatch):
    class Stalled:
        returncode = None
        killed = False

        async def communicate(self, data=None):
            await asyncio.sleep(3600)

        def kill(self):
            self.killed = True

        async def wait(self):
            await asyncio.sleep(3600)

    process = Stalled()

    async def spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wtype")
    monkeypatch.setattr(linux.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(linux, "_NIRI_WTYPE_TIMEOUT_S", 0.02)
    monkeypatch.setattr(linux, "_NIRI_WTYPE_REAP_TIMEOUT_S", 0.02)
    with pytest.raises(GuiUnavailableError, match="timed out"):
        await asyncio.wait_for(
            linux._run_niri_wtype(["-k", "a"], dict(ENV)), timeout=1
        )
    assert process.killed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("portal", "wtype", "keyboard", "pointer", "source"),
    [
        (0, True, True, False, "niri-virtual-keyboard"),
        (2, True, True, True, "niri-virtual-keyboard+xdg-desktop-portal"),
        (2, False, False, True, "xdg-desktop-portal"),
        (0, False, False, False, None),
    ],
)
async def test_niri_mixed_input_provider_reports_only_available_transports(
    monkeypatch, portal, wtype, keyboard, pointer, source
):
    async def portal_devices(_env):
        return portal

    backend = linux.LinuxGuiBackend(niri_native_keyboard_enabled=True)
    backend._env = dict(ENV)
    monkeypatch.setattr(linux, "portal_available_devices", portal_devices)
    monkeypatch.setattr(
        linux.shutil,
        "which",
        lambda binary: (
            "/usr/bin/wtype" if wtype and binary == "wtype" else None
        ),
    )
    monkeypatch.setattr(backend, "_list_data", lambda _env: {"windows": []})
    result = (await backend.list_windows())["capabilities"]
    assert result["keyboard_input"] is keyboard
    assert result["coordinate_input"] is pointer
    assert result["wayland_input"] == source
