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
            },
            "modes": [{"width": 1920, "height": 1080}],
            "current_mode": 0,
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


def test_drag_cancellation_is_opt_in_only_for_niri_pointer():
    pointer = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    pointer._env = dict(ENV)
    assert pointer.cancel_safe_drag_input is True
    assert pointer.cancel_safe_keyboard_input is False
    pointer_disabled = linux.LinuxGuiBackend(niri_native_pointer_enabled=False)
    pointer_disabled._env = dict(ENV)
    assert pointer_disabled.cancel_safe_drag_input is False
    pointer._env = {**ENV, "XDG_CURRENT_DESKTOP": "GNOME"}
    assert pointer.cancel_safe_drag_input is False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "move", "cancel", "stale"])
async def test_drag_always_releases_button(monkeypatch, failure):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    commands = []
    cancel = asyncio.Event()
    focus_checks = 0

    async def focused(*_args):
        nonlocal focus_checks
        focus_checks += 1
        if failure == "stale" and focus_checks == 3:
            raise GuiStaleStateError("Window lost focus during drag")

    async def client(_env, *args, capture=False):
        if capture:
            return deepcopy(CLIENT_OUTPUTS)
        commands.append(args)
        if args[0] == "mousedown" and failure == "cancel":
            cancel.set()
        if args[0] == "mousemove" and len(commands) == 3 and failure == "move":
            raise GuiUnavailableError("Failed to move pointer")

    monkeypatch.setattr(
        linux, "_niri_json", lambda _env, what: deepcopy(COMPOSITOR[what])
    )
    monkeypatch.setattr(linux, "_assert_niri_window_focused", focused)
    monkeypatch.setattr(linux, "_wdotool", client)
    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")
    action = {
        "type": "drag",
        "x": 100,
        "y": 100,
        "to_x": 150,
        "to_y": 125,
        "_cancel_event": cancel,
    }
    if failure is None:
        result = await backend._perform_niri_pointer(
            WINDOW, None, action, backend._env
        )
        assert result == {
            "from": {"x": 100, "y": 100},
            "to": {"x": 150, "y": 125},
            "method": "niri-virtual-pointer",
        }
    else:
        error = (
            asyncio.CancelledError
            if failure == "cancel"
            else (
                GuiStaleStateError
                if failure == "stale"
                else GuiUnavailableError
            )
        )
        with pytest.raises(error):
            await backend._perform_niri_pointer(
                WINDOW, None, action, backend._env
            )
    assert commands[0] == ("mousemove", "--output", "eDP-1", "900", "537")
    assert commands[1] == ("mousedown", "1")
    assert commands[-1] == ("mouseup", "1")
    if failure is None:
        assert commands[2] == ("mousemove", "--output", "eDP-1", "950", "562")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_during", ["inventory", "pressed_focus"])
async def test_drag_cancel_during_async_validation_never_moves_after_cancel(
    monkeypatch, cancel_during
):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    cancel = asyncio.Event()
    entered, resume = asyncio.Event(), asyncio.Event()
    commands = []
    focus_checks = 0

    async def focused(*_args):
        nonlocal focus_checks
        focus_checks += 1
        if cancel_during == "pressed_focus" and focus_checks == 3:
            entered.set()
            await resume.wait()

    async def client(_env, *args, capture=False):
        if capture:
            if cancel_during == "inventory":
                entered.set()
                await resume.wait()
            return deepcopy(CLIENT_OUTPUTS)
        commands.append(args)

    monkeypatch.setattr(
        linux, "_niri_json", lambda _env, key: deepcopy(COMPOSITOR[key])
    )
    monkeypatch.setattr(linux, "_assert_niri_window_focused", focused)
    monkeypatch.setattr(linux, "_wdotool", client)
    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")
    task = asyncio.create_task(
        backend._perform_niri_pointer(
            WINDOW,
            None,
            {
                "type": "drag",
                "x": 100,
                "y": 100,
                "to_x": 150,
                "to_y": 125,
                "_cancel_event": cancel,
            },
            backend._env,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    cancel.set()
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert commands == (
        []
        if cancel_during == "inventory"
        else [
            ("mousemove", "--output", "eDP-1", "900", "537"),
            ("mousedown", "1"),
            ("mouseup", "1"),
        ]
    )


@pytest.mark.asyncio
async def test_pre_cancelled_drag_never_moves_or_releases_mouse(monkeypatch):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    cancelled = asyncio.Event()
    cancelled.set()

    async def forbidden(*_args, **_kwargs):
        pytest.fail("Cancelled drag must never reach wdotool")

    monkeypatch.setattr(linux, "_wdotool", forbidden)
    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")
    with pytest.raises(asyncio.CancelledError):
        await backend._perform_niri_pointer(
            WINDOW,
            None,
            {
                "type": "drag",
                "x": 100,
                "y": 100,
                "to_x": 150,
                "to_y": 125,
                "_cancel_event": cancelled,
            },
            backend._env,
        )


@pytest.mark.asyncio
async def test_drag_task_cancellation_still_sends_mouseup(monkeypatch):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    pressed, released = asyncio.Event(), asyncio.Event()

    async def client(_env, *args, capture=False):
        if capture:
            return deepcopy(CLIENT_OUTPUTS)
        if args[0] == "mousedown":
            pressed.set()
        elif args[0] == "mouseup":
            released.set()

    async def focused(*_args):
        return None

    monkeypatch.setattr(
        linux, "_niri_json", lambda _env, name: deepcopy(COMPOSITOR[name])
    )
    monkeypatch.setattr(linux, "_assert_niri_window_focused", focused)
    monkeypatch.setattr(linux, "_wdotool", client)
    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")
    task = asyncio.create_task(
        backend._perform_niri_pointer(
            WINDOW,
            None,
            {"type": "drag", "x": 100, "y": 100, "to_x": 150, "to_y": 125},
            backend._env,
        )
    )
    await asyncio.wait_for(pressed.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert released.is_set()


@pytest.mark.asyncio
async def test_changed_output_transform_prevents_click(monkeypatch):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    output_queries = 0
    commands = []

    def inventory(_env, key):
        nonlocal output_queries
        data = deepcopy(COMPOSITOR[key])
        if key == "outputs":
            output_queries += 1
            if output_queries == 2:
                data["eDP-1"]["logical"]["transform"] = "180"
        return data

    async def focused(*_args):
        pass

    async def client(_env, *args, capture=False):
        if capture:
            return deepcopy(CLIENT_OUTPUTS)
        commands.append(args)

    monkeypatch.setattr(linux, "_niri_json", inventory)
    monkeypatch.setattr(linux, "_assert_niri_window_focused", focused)
    monkeypatch.setattr(linux, "_wdotool", client)
    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")
    with pytest.raises(GuiStaleStateError, match="moved"):
        await backend._perform_niri_pointer(
            WINDOW, None, {"type": "click", "x": 100, "y": 100}, backend._env
        )
    assert commands == [("mousemove", "--output", "eDP-1", "900", "537")]


@pytest.mark.asyncio
async def test_drag_rejects_invalid_destination_before_any_pointer_input(
    monkeypatch,
):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    monkeypatch.setattr(
        linux, "_niri_json", lambda _env, what: deepcopy(COMPOSITOR[what])
    )
    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")

    async def unexpected(*_args, **_kwargs):
        pytest.fail("Invalid drag must not inject pointer events")

    monkeypatch.setattr(linux, "_wdotool", unexpected)
    with pytest.raises(ValueError, match="outside"):
        await backend._perform_niri_pointer(
            WINDOW,
            None,
            {"type": "drag", "x": 1, "y": 1, "to_x": 400, "to_y": 3},
            backend._env,
        )


@pytest.mark.asyncio
async def test_fractional_scale_pointer_uses_actual_wayland_integer_scale(
    monkeypatch,
):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    compositor = deepcopy(COMPOSITOR)
    compositor["windows"][0]["layout"]["tile_pos_in_workspace_view"] = [
        100,
        100,
    ]
    logical = compositor["outputs"]["eDP-1"]["logical"]
    logical.update({"scale": 1.25, "width": 1024, "height": 768})
    compositor["outputs"]["eDP-1"]["modes"][0].update(
        {"width": 1280, "height": 960}
    )
    commands = []

    async def client(_env, *args, capture=False):
        if capture:
            return [
                {
                    "name": "eDP-1",
                    "x": 0,
                    "y": 0,
                    "width": 1024,
                    "height": 768,
                    "scale": 2,
                }
            ]
        commands.append(args)

    async def focused(*_args):
        return None

    monkeypatch.setattr(
        linux, "_niri_json", lambda _env, key: deepcopy(compositor[key])
    )
    monkeypatch.setattr(linux, "_assert_niri_window_focused", focused)
    monkeypatch.setattr(linux, "_wdotool", client)
    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")
    await backend._perform_niri_pointer(
        WINDOW, None, {"type": "move", "x": 100, "y": 100}, backend._env
    )
    assert commands == [("mousemove", "--output", "eDP-1", "250", "250")]


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["click", "right_click", "double_click", "move", "scroll"]
)
async def test_niri_element_only_pointer_uses_validated_center(
    monkeypatch, kind
):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    commands = []

    async def focused(*_args):
        return None

    async def wdotool(_env, *args, capture=False):
        commands.append(args)
        return deepcopy(CLIENT_OUTPUTS) if capture else None

    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")
    monkeypatch.setattr(
        linux, "_niri_json", lambda _env, name: deepcopy(COMPOSITOR[name])
    )
    monkeypatch.setattr(linux, "_assert_niri_window_focused", focused)
    monkeypatch.setattr(linux, "_wdotool", wdotool)
    result = await backend._perform_niri_pointer(
        WINDOW,
        {"bounds": {"x": 30, "y": 40, "width": 20, "height": 30}},
        {"type": kind, "element_id": "e1"},
        backend._env,
    )
    assert result == {"x": 40, "y": 55, "method": "niri-virtual-pointer"}
    assert ("mousemove", "--output", "eDP-1", "840", "492") in commands


@pytest.mark.asyncio
async def test_niri_element_only_click_semantic_first_then_pointer(monkeypatch):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    commands = []
    locator = {
        "semantic": {
            "path": [0],
            "accessible_id": "id",
            "fingerprint": "stable",
        }
    }

    def helper(payload, _env=None):
        commands.append(("helper", payload["command"]))
        if payload["command"] == "semantic_action":
            raise linux._SemanticActionUnavailableError(
                "no AT-SPI action interface"
            )
        assert payload["locator"] is locator["semantic"]
        return {"bounds": {"x": 30, "y": 40, "width": 20, "height": 30}}

    async def focused(*_args, **_kwargs):
        return None

    async def wdotool(_env, *args, capture=False):
        commands.append(args)
        return deepcopy(CLIENT_OUTPUTS) if capture else None

    monkeypatch.setattr(backend, "_helper", helper)
    monkeypatch.setattr(backend, "_focus_window", focused)
    monkeypatch.setattr(linux, "_assert_niri_window_focused", focused)
    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")
    monkeypatch.setattr(
        linux, "_niri_json", lambda _env, name: deepcopy(COMPOSITOR[name])
    )
    monkeypatch.setattr(linux, "_wdotool", wdotool)
    result = await backend.perform_action(
        WINDOW, locator, {"type": "click", "element_id": "e1"}
    )
    assert result == {"x": 40, "y": 55, "method": "niri-virtual-pointer"}
    assert commands[:2] == [
        ("helper", "semantic_action"),
        ("helper", "resolve_locator"),
    ]
    assert ("mousemove", "--output", "eDP-1", "840", "492") in commands
    assert ("click", "1") in commands


@pytest.mark.asyncio
async def test_niri_element_click_prefers_semantic_and_never_synthesizes(
    monkeypatch,
):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    seen = []

    def helper(payload, _env=None):
        seen.append(payload["command"])
        return {"semantic": True}

    async def no_pointer(*_args, **_kwargs):
        pytest.fail(
            "Native pointer must not run after successful semantic click"
        )

    monkeypatch.setattr(backend, "_helper", helper)
    monkeypatch.setattr(backend, "_perform_niri_pointer", no_pointer)
    result = await backend.perform_action(
        WINDOW,
        {"semantic": {"path": [0]}},
        {"type": "click", "element_id": "e1"},
    )
    assert result == {"semantic": True}
    assert seen == ["semantic_action"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bounds",
    [
        {"x": 30, "y": 40, "width": 0, "height": 10},
        {"x": 310, "y": 220, "width": 30, "height": 50},
        {"x": -40, "y": 5, "width": 20, "height": 20},
        {"y": 40, "width": 20, "height": 20},
        {"x": 30, "width": 20, "height": 20},
    ],
)
async def test_niri_element_bad_bounds_rejected_before_pointer(
    monkeypatch, bounds
):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)

    async def no_pointer(*_args, **_kwargs):
        pytest.fail(
            "Invalid element bounds must not inject any native pointer events"
        )

    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")
    monkeypatch.setattr(linux, "_wdotool", no_pointer)
    with pytest.raises(ValueError):
        await backend._perform_niri_pointer(
            WINDOW,
            {"bounds": bounds},
            {"type": "click", "element_id": "e1"},
            backend._env,
        )


@pytest.mark.asyncio
async def test_niri_element_with_explicit_coordinates_uses_them_instead_of_center(
    monkeypatch,
):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    commands = []

    async def focused(*_args):
        return None

    async def wdotool(_env, *args, capture=False):
        commands.append(args)
        return deepcopy(CLIENT_OUTPUTS) if capture else None

    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")
    monkeypatch.setattr(
        linux, "_niri_json", lambda _env, name: deepcopy(COMPOSITOR[name])
    )
    monkeypatch.setattr(linux, "_assert_niri_window_focused", focused)
    monkeypatch.setattr(linux, "_wdotool", wdotool)
    result = await backend._perform_niri_pointer(
        WINDOW,
        {"bounds": {"x": 30, "y": 40, "width": 20, "height": 30}},
        {"type": "click", "element_id": "e1", "x": 100, "y": 110},
        backend._env,
    )
    assert result["x"] == 100 and result["y"] == 110
    assert ("mousemove", "--output", "eDP-1", "900", "547") in commands


@pytest.mark.asyncio
async def test_niri_element_screen_bounds_convert_using_nonzero_window_origin(
    monkeypatch,
):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    window = deepcopy(WINDOW)
    window["bounds"]["x"] = 50
    window["bounds"]["y"] = 60
    commands = []

    async def focused(*_args):
        return None

    async def wdotool(_env, *args, capture=False):
        commands.append(args)
        return deepcopy(CLIENT_OUTPUTS) if capture else None

    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/wdotool")
    monkeypatch.setattr(
        linux, "_niri_json", lambda _env, name: deepcopy(COMPOSITOR[name])
    )
    monkeypatch.setattr(linux, "_assert_niri_window_focused", focused)
    monkeypatch.setattr(linux, "_wdotool", wdotool)
    result = await backend._perform_niri_pointer(
        window,
        {"bounds": {"x": 80, "y": 90, "width": 20, "height": 20}},
        {"type": "click", "element_id": "e1"},
        backend._env,
    )
    assert result["x"] == 40 and result["y"] == 40
    assert ("mousemove", "--output", "eDP-1", "840", "477") in commands


@pytest.mark.asyncio
async def test_niri_stale_element_identity_never_sends_pointer(monkeypatch):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)

    def helper(payload, _env=None):
        if payload["command"] == "semantic_action":
            raise linux._SemanticActionUnavailableError(
                "no AT-SPI action interface"
            )
        raise GuiStaleStateError(
            "AT-SPI target element changed since observation"
        )

    async def no_pointer(*_args, **_kwargs):
        pytest.fail("Stale element must not send virtual pointer input")

    monkeypatch.setattr(backend, "_helper", helper)
    monkeypatch.setattr(linux, "_wdotool", no_pointer)
    monkeypatch.setattr(
        linux, "_niri_json", lambda *_: pytest.fail("No compositor operation")
    )
    with pytest.raises(GuiStaleStateError, match="changed"):
        await backend.perform_action(
            WINDOW,
            {"semantic": {"path": [0], "accessible_id": "stale"}},
            {"type": "click", "element_id": "e1"},
        )


@pytest.mark.asyncio
async def test_niri_failed_semantic_click_does_not_silently_retry_as_pointer(
    monkeypatch,
):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)

    def helper(payload, _env=None):
        raise GuiUnavailableError("AT-SPI activation rejected the action")

    async def no_pointer(*_args, **_kwargs):
        pytest.fail(
            "Failed semantic click must not be retried as a native click"
        )

    monkeypatch.setattr(backend, "_helper", helper)
    monkeypatch.setattr(linux, "_wdotool", no_pointer)
    with pytest.raises(GuiUnavailableError, match="activation rejected"):
        await backend.perform_action(
            WINDOW,
            {"semantic": {"path": [0]}},
            {"type": "click", "element_id": "e1"},
        )


@pytest.mark.asyncio
async def test_niri_semantic_click_fallback_still_needs_window_activation(
    monkeypatch,
):
    backend = linux.LinuxGuiBackend(niri_native_pointer_enabled=True)
    backend._env = dict(ENV)
    commands = []

    def helper(payload, _env=None):
        commands.append(payload["command"])
        if payload["command"] == "semantic_action":
            raise linux._SemanticActionUnavailableError(
                "no AT-SPI action interface"
            )
        return {"bounds": {"x": 30, "y": 40, "width": 20, "height": 30}}

    async def cannot_activate(*_args, **_kwargs):
        commands.append("activate")
        raise GuiUnavailableError("Target window activation unavailable")

    async def no_input(*_args, **_kwargs):
        pytest.fail("Native pointer may not run if activation fails")

    monkeypatch.setattr(backend, "_helper", helper)
    monkeypatch.setattr(backend, "_focus_window", cannot_activate)
    monkeypatch.setattr(backend, "_perform_niri_pointer", no_input)
    with pytest.raises(GuiUnavailableError, match="activation unavailable"):
        await backend.perform_action(
            WINDOW,
            {"semantic": {"path": [0], "accessible_id": "stable"}},
            {"type": "click", "element_id": "e1"},
        )
    assert commands == ["semantic_action", "resolve_locator", "activate"]
