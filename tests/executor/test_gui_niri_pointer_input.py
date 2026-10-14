"""Niri native pointer remains default-off, target-bound and never falls back to uinput."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.errors import GuiStaleStateError, GuiUnavailableError
from workgate.executor.gui import linux

ENV = {
    "XDG_SESSION_TYPE": "wayland",
    "XDG_CURRENT_DESKTOP": "niri",
    "NIRI_SOCKET": "/run/user/1000/niri.sock",
    "WAYLAND_DISPLAY": "wayland-1",
}
WINDOW = {
    "id": "atspi:47:stable",
    "pid": 47,
    "title": "Disposable",
    "bounds": {"x": 0, "y": 0, "width": 320, "height": 240},
}
COMPOSITOR = {
    "windows": [
        {
            "id": 77,
            "pid": 47,
            "title": "Disposable",
            "is_focused": True,
            "workspace_id": 1,
            "layout": {
                "window_size": [320, 240],
                "tile_pos_in_workspace_view": [800.0, 437.0],
                "window_offset_in_tile": [0, 0],
            },
        }
    ],
    "workspaces": [{"id": 1, "output": "eDP-1", "is_active": True}],
    "outputs": {
        "eDP-1": {
            "logical": {
                "x": 0,
                "y": 0,
                "width": 1920,
                "height": 1080,
                "scale": 1.0,
                "transform": "Normal",
            }
        }
    },
}
CLIENT_OUTPUTS = [
    {"name": "eDP-1", "width": 1920, "height": 1080, "scale": 1, "x": 0, "y": 0}
]


def test_pointer_opt_in_is_independent_and_disabled_by_default(monkeypatch):
    monkeypatch.delenv(
        "WORKGATE_GUI_NIRI_NATIVE_POINTER_ENABLED", raising=False
    )
    assert Settings().gui_niri_native_pointer_enabled is False
    assert (
        resolve_executor_config(Settings()).gui_niri_native_pointer_enabled
        is False
    )
    configured = resolve_executor_config(
        Settings(gui_niri_native_pointer_enabled=True)
    )
    assert configured.gui_niri_native_pointer_enabled is True
    assert configured.gui_niri_native_keyboard_enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "keyboard,pointer",
    [(False, False), (True, False), (False, True), (True, True)],
)
async def test_pointer_and_keyboard_independent_provider_selection(
    monkeypatch, keyboard, pointer
):
    async def no_portal(_env):
        return 0

    monkeypatch.setattr(linux, "portal_available_devices", no_portal)
    monkeypatch.setattr(linux.shutil, "which", lambda name: "/usr/bin/" + name)
    backend = linux.LinuxGuiBackend(
        niri_native_keyboard_enabled=keyboard,
        niri_native_pointer_enabled=pointer,
    )
    backend._env = dict(ENV)
    monkeypatch.setattr(backend, "_list_data", lambda _env: {"windows": []})
    capabilities = (await backend.list_windows())["capabilities"]
    assert capabilities["coordinate_input"] is pointer
    assert capabilities["keyboard_input"] is keyboard
    if keyboard and pointer:
        assert (
            capabilities["wayland_input"]
            == "niri-virtual-keyboard+niri-virtual-pointer"
        )
    ops = backend._desktop_operations(ENV)
    assert getattr(ops.input, "__func__", None) is (
        linux.LinuxGuiBackend._perform_niri_input
        if (pointer or keyboard)
        else linux.LinuxGuiBackend._perform_wayland
    )


@pytest.mark.asyncio
async def test_pointer_absent_binary_does_not_fall_back_to_portal(monkeypatch):
    async def portal_devices(_env):
        return 2

    monkeypatch.setattr(linux, "portal_available_devices", portal_devices)
    monkeypatch.setattr(linux.shutil, "which", lambda _: None)
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    monkeypatch.setattr(backend, "_list_data", lambda _env: {"windows": []})
    report = (await backend.list_windows())["capabilities"]
    assert not report["coordinate_input"]
    assert report["wayland_input"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,extras,expected",
    [
        ("move", {}, []),
        ("click", {}, [("click", "1")]),
        ("right_click", {}, [("click", "3")]),
        ("double_click", {}, [("click", "1"), ("click", "1")]),
        ("scroll", {"delta_y": -3, "delta_x": 2}, [("scroll", "2", "-3")]),
    ],
)
async def test_pointer_actions_are_mapped_and_target_checked(
    monkeypatch, kind, extras, expected
):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    calls = []

    def inventory(_env, what):
        return deepcopy(COMPOSITOR[what])

    async def focused(*_args):
        calls.append(("focused",))

    async def client(_env, *args, capture=False):
        if capture:
            calls.append(("outputs",))
            return deepcopy(CLIENT_OUTPUTS)
        calls.append(args)

    monkeypatch.setattr(linux, "_niri_json", inventory)
    monkeypatch.setattr(linux, "_assert_niri_window_focused", focused)
    monkeypatch.setattr(linux, "_wdotool", client)
    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")
    result = await backend._perform_niri_pointer(
        WINDOW, None, {"type": kind, "x": 100, "y": 100, **extras}, backend._env
    )
    assert result["method"] == "niri-virtual-pointer"
    assert ("mousemove", "--output", "eDP-1", "900", "537") in calls
    assert calls[0] == ("focused",)
    assert calls[1] == ("outputs",)
    assert [x for x in calls if x and x[0] in ("click", "scroll")] == expected
    assert calls[-1] == ("focused",)


@pytest.mark.asyncio
async def test_pointer_refuses_stale_focus_before_any_input(monkeypatch):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    compositor = deepcopy(COMPOSITOR)
    compositor["windows"][0]["is_focused"] = False
    monkeypatch.setattr(linux, "_niri_json", lambda _env, key: compositor[key])
    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")

    async def unexpected(*_args, **_kwargs):
        pytest.fail("Must not move the mouse with an unfocused target")

    monkeypatch.setattr(linux, "_wdotool", unexpected)
    with pytest.raises(GuiStaleStateError, match="focused"):
        await backend._perform_niri_pointer(
            WINDOW, None, {"type": "click", "x": 1, "y": 1}, backend._env
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("width", 1080),
        ("height", 1920),
        ("x", 120),
        ("y", -100),
        ("scale", 1.25),
    ],
)
async def test_pointer_rejects_output_disagreement_before_movement(
    monkeypatch, field, value
):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    commands = []
    monkeypatch.setattr(
        linux, "_niri_json", lambda _env, key: deepcopy(COMPOSITOR[key])
    )
    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")

    async def focused(*_args):
        pass

    async def client(_env, *args, capture=False):
        commands.append(args)
        if capture:
            outputs = deepcopy(CLIENT_OUTPUTS)
            outputs[0][field] = value
            return outputs
        pytest.fail("Pointer must not move if outputs disagree")

    monkeypatch.setattr(linux, "_assert_niri_window_focused", focused)
    monkeypatch.setattr(linux, "_wdotool", client)
    with pytest.raises(GuiUnavailableError, match="geometry disagree"):
        await backend._perform_niri_pointer(
            WINDOW, None, {"type": "click", "x": 1, "y": 1}, backend._env
        )
    assert commands == [("outputs", "--json")]


@pytest.mark.asyncio
async def test_drag_rejected_before_any_window_focus(monkeypatch):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    focus = []

    async def focused(*_args, **_kwargs):
        focus.append(True)

    monkeypatch.setattr(backend, "_focus_window", focused)
    with pytest.raises(GuiUnavailableError, match="drag"):
        await backend.perform_action(
            WINDOW, None, {"type": "drag", "x": 1, "y": 1, "to_x": 3, "to_y": 3}
        )
    assert focus == []


@pytest.mark.asyncio
async def test_pointer_runtime_forces_wlr_protocols_without_uinput_fallback(
    monkeypatch,
):
    process = SimpleNamespace(returncode=0)
    seen = []

    async def communicate():
        return b"", b""

    process.communicate = communicate

    async def launch(*argv, **_kwargs):
        seen.append(argv)
        return process

    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")
    monkeypatch.setattr(linux.asyncio, "create_subprocess_exec", launch)
    await linux._wdotool(dict(ENV), "click", "1")
    assert seen == [
        ("/usr/bin/wdotool", "--backend", "wlr-protocols", "click", "1")
    ]


@pytest.mark.asyncio
async def test_pointer_rechecks_compositor_position_after_moving_before_click(
    monkeypatch,
):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    queried = 0
    executed = []

    def inventory(_env, name):
        nonlocal queried
        data = deepcopy(COMPOSITOR[name])
        if name == "windows":
            queried += 1
            if queried == 2:
                data[0]["layout"]["tile_pos_in_workspace_view"] = [100.0, 100.0]
        return data

    async def focused(*_args):
        return None

    async def client(_env, *args, capture=False):
        executed.append(args)
        return deepcopy(CLIENT_OUTPUTS) if capture else None

    monkeypatch.setattr(linux, "_niri_json", inventory)
    monkeypatch.setattr(linux, "_assert_niri_window_focused", focused)
    monkeypatch.setattr(linux, "_wdotool", client)
    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")
    with pytest.raises(GuiStaleStateError, match="moved"):
        await backend._perform_niri_pointer(
            WINDOW, None, {"type": "click", "x": 100, "y": 100}, backend._env
        )
    assert executed == [
        ("outputs", "--json"),
        ("mousemove", "--output", "eDP-1", "900", "537"),
    ]


@pytest.mark.asyncio
async def test_pointer_does_not_click_if_focus_changes_after_moving(
    monkeypatch,
):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    commands = []
    focus_checks = 0

    async def focused(*_args):
        nonlocal focus_checks
        focus_checks += 1
        if focus_checks == 2:
            raise GuiStaleStateError("Focus moved during pointer action")

    async def client(_env, *args, capture=False):
        commands.append(args)
        return deepcopy(CLIENT_OUTPUTS) if capture else None

    monkeypatch.setattr(
        linux, "_niri_json", lambda _env, name: deepcopy(COMPOSITOR[name])
    )
    monkeypatch.setattr(linux, "_assert_niri_window_focused", focused)
    monkeypatch.setattr(linux, "_wdotool", client)
    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")
    with pytest.raises(GuiStaleStateError, match="Focus moved"):
        await backend._perform_niri_pointer(
            WINDOW, None, {"type": "click", "x": 120, "y": 80}, backend._env
        )
    assert focus_checks == 2
    assert commands == [
        ("outputs", "--json"),
        ("mousemove", "--output", "eDP-1", "920", "517"),
    ]


@pytest.mark.asyncio
async def test_both_niri_native_devices_do_not_query_portal(monkeypatch):
    backend = linux.LinuxGuiBackend(
        niri_native_keyboard_enabled=True, niri_native_pointer_enabled=True
    )
    backend._env = dict(ENV)

    async def forbidden_portal(_env):
        pytest.fail("Both native input devices must not depend on Portal")

    monkeypatch.setattr(linux, "portal_available_devices", forbidden_portal)
    monkeypatch.setattr(linux.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(backend, "_list_data", lambda _env: {"windows": []})
    cap = (await backend.list_windows())["capabilities"]
    assert cap["keyboard_input"] and cap["coordinate_input"]
    assert cap["wayland_input"] == "niri-virtual-keyboard+niri-virtual-pointer"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_native_pointer_process_reaped_after_abort(monkeypatch, cancel):
    started = asyncio.Event()

    class Child:
        returncode = None
        killed = False

        async def communicate(self):
            started.set()
            await asyncio.sleep(3600)

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    child = Child()

    async def spawn(*_argv, **_kwargs):
        return child

    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")
    monkeypatch.setattr(linux.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(linux, "_NIRI_POINTER_COMMAND_TIMEOUT_S", 0.02)
    task = asyncio.create_task(
        linux._wdotool(dict(ENV), "mousemove", "50", "60")
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    else:
        with pytest.raises(GuiUnavailableError, match="timed out"):
            await asyncio.wait_for(task, timeout=1)
    assert child.killed
