"""Niri and portal regressions: safe targeting and honest capabilities."""

import json
from types import SimpleNamespace

import pytest

from workgate.errors import GuiStaleStateError, GuiUnavailableError
from workgate.executor.gui import linux, linux_portal

WINDOW = {
    "id": "atspi:47:fingerprint",
    "pid": 47,
    "title": "Test dialog",
    "bounds": {"x": 1, "y": 2, "width": 100, "height": 50},
}
ENV = {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "niri"}


@pytest.mark.asyncio
async def test_niri_focus_uses_compositor_id_not_atspi_grab_focus(monkeypatch):
    commands = []

    def run(argv, **_kwargs):
        commands.append(argv)
        if argv[-1] == "windows":
            output = [{"id": 88, "pid": 47, "title": "Test dialog"}]
        elif argv[-1] == "focused-window":
            output = {"id": 88, "pid": 47, "title": "Test dialog"}
        else:
            output = None
        return SimpleNamespace(stdout=json.dumps(output))

    monkeypatch.setattr(linux.subprocess, "run", run)
    backend = linux.LinuxGuiBackend()
    monkeypatch.setattr(
        backend,
        "_helper",
        lambda *_: pytest.fail("AT-SPI focus must not be used"),
    )
    await backend._focus_window(WINDOW, dict(ENV))
    assert [cmd[:4] for cmd in commands] == [
        ["niri", "msg", "-j", "windows"],
        ["niri", "msg", "action", "focus-window"],
        ["niri", "msg", "-j", "focused-window"],
    ]
    assert commands[1][-1] == "88"


@pytest.mark.asyncio
async def test_niri_ambiguous_match_fails_before_focus(monkeypatch):
    def run(_argv, **_kwargs):
        return SimpleNamespace(
            stdout=json.dumps(
                [
                    {"id": 88, "pid": 47, "title": "Test dialog"},
                    {"id": 89, "pid": 47, "title": "Test dialog"},
                ]
            )
        )

    monkeypatch.setattr(linux.subprocess, "run", run)
    with pytest.raises(GuiStaleStateError, match="uniquely match"):
        await linux.LinuxGuiBackend()._focus_window(WINDOW, dict(ENV))


@pytest.mark.asyncio
async def test_niri_activation_of_other_window_fails_closed(monkeypatch):
    def run(argv, **_kwargs):
        if argv[-1] == "windows":
            output = [{"id": 88, "pid": 47, "title": "Test dialog"}]
        elif argv[-1] == "focused-window":
            output = {"id": 89, "pid": 47, "title": "Other dialog"}
        else:
            output = None
        return SimpleNamespace(stdout=json.dumps(output))

    monkeypatch.setattr(linux.subprocess, "run", run)
    with pytest.raises(GuiStaleStateError, match="different window"):
        await linux.LinuxGuiBackend()._focus_window(WINDOW, dict(ENV))


@pytest.mark.asyncio
async def test_niri_missing_input_devices_no_fake_capabilities_or_focus(
    monkeypatch,
):
    backend = linux.LinuxGuiBackend()
    backend._env = dict(ENV)

    async def no_devices(_env):
        return 0

    monkeypatch.setattr(linux, "portal_available_devices", no_devices)
    monkeypatch.setattr(backend, "_list_data", lambda *_: {"windows": [WINDOW]})
    monkeypatch.setattr(
        backend,
        "_focus_window",
        lambda *_: pytest.fail("Must not change focus"),
    )
    report = await backend.list_windows()
    assert report["capabilities"]["window_capture"] is False
    assert report["capabilities"]["coordinate_input"] is False
    assert report["capabilities"]["keyboard_input"] is False
    with pytest.raises(GuiUnavailableError, match="pointer input"):
        await backend.focus_window(WINDOW)
    with pytest.raises(GuiUnavailableError, match="keyboard input"):
        await backend.perform_action(
            WINDOW, None, {"type": "key", "keys": "Return"}
        )


@pytest.mark.asyncio
async def test_semantic_click_does_not_require_window_activation(monkeypatch):
    backend = linux.LinuxGuiBackend()
    backend._env = dict(ENV)
    monkeypatch.setattr(
        backend,
        "_focus_window",
        lambda *_: pytest.fail("Must not change focus"),
    )
    monkeypatch.setattr(backend, "_helper", lambda *_: {"semantic": True})
    result = await backend.perform_action(
        WINDOW,
        {"semantic": {"path": [0], "accessible_id": "id"}},
        {"type": "click", "element_id": "e1"},
    )
    assert result["semantic"] is True


def test_portal_skips_unrelated_unparseable_introspection():
    xml = """<node><interface name="org.freedesktop.portal.Screenshot">
      <method name="Screenshot"><arg type="s" direction="in"/><arg type="a{sv}" direction="in"/><arg type="o" direction="out"/></method>
      </interface><interface name="org.freedesktop.portal.PowerProfileMonitor">
      <property name="power-saver-enabled" type="b" access="read"/>
      </interface></node>"""
    filtered = linux_portal._portal_interface_xml(
        xml, "org.freedesktop.portal.Screenshot"
    )
    assert 'name="org.freedesktop.portal.Screenshot"' in filtered
    assert "PowerProfileMonitor" not in filtered
    assert "power-saver-enabled" not in filtered


@pytest.mark.asyncio
async def test_portal_no_remote_devices_fails_before_creating_session(
    monkeypatch,
):
    async def no_devices(_env):
        return 0

    async def should_not_connect():
        pytest.fail("No permission to start RemoteDesktop")

    monkeypatch.setattr(linux_portal, "portal_available_devices", no_devices)
    portal = linux_portal.PortalDesktop(dict(ENV))
    monkeypatch.setattr(portal, "_connect", should_not_connect)
    with pytest.raises(GuiUnavailableError, match="no input devices"):
        await portal.ensure_session()


@pytest.mark.asyncio
async def test_missing_optional_dbus_next_keeps_gui_inventory_available(
    monkeypatch,
):
    def missing_modules():
        raise GuiUnavailableError(
            "Wayland GUI control requires the dbus-next package"
        )

    monkeypatch.setattr(linux_portal, "_portal_modules", missing_modules)
    assert await linux_portal.portal_available_devices(dict(ENV)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("devices", "pointer", "keyboard"),
    [(0, False, False), (1, False, True), (2, True, False), (3, True, True)],
)
async def test_wayland_remote_desktop_device_bits(
    monkeypatch, devices, pointer, keyboard
):
    backend = linux.LinuxGuiBackend()
    backend._env = {
        "XDG_SESSION_TYPE": "wayland",
        "XDG_CURRENT_DESKTOP": "other",
    }
    monkeypatch.setattr(
        backend, "_list_data", lambda _env: {"windows": [WINDOW]}
    )

    async def provide(_env):
        return devices

    monkeypatch.setattr(linux, "portal_available_devices", provide)
    report = await backend.list_windows()
    assert report["capabilities"]["coordinate_input"] == pointer
    assert report["capabilities"]["keyboard_input"] == keyboard


@pytest.mark.asyncio
async def test_niri_native_capture_is_bound_to_single_window(
    tmp_path, monkeypatch
):
    from PIL import Image

    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[-1] == "windows":
            return SimpleNamespace(
                stdout=json.dumps(
                    [{"id": 88, "pid": 47, "title": "Test dialog"}]
                )
            )
        if "screenshot-window" in argv:
            dest = argv[argv.index("--path") + 1]
            Image.new("RGB", (100, 50), "white").save(dest)
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(linux.subprocess, "run", run)
    capture = tmp_path / "capture.png"
    result = await linux._capture_niri_window(capture, WINDOW, dict(ENV))
    assert result == "niri-ipc"
    assert capture.is_file()
    cmd = next(item for item in calls if "screenshot-window" in item)
    assert cmd[cmd.index("--id") + 1] == "88"
    assert cmd[cmd.index("--path") + 1] == str(capture)
    assert cmd[cmd.index("--show-pointer") + 1] == "false"


@pytest.mark.asyncio
async def test_niri_native_capture_rejects_retargeted_window(
    tmp_path, monkeypatch
):
    from PIL import Image

    list_count = 0

    def run(argv, **_kwargs):
        nonlocal list_count
        if argv[-1] == "windows":
            list_count += 1
            return SimpleNamespace(
                stdout=json.dumps(
                    [
                        {
                            "id": 88 if list_count == 1 else 89,
                            "pid": 47,
                            "title": "Test dialog",
                        }
                    ]
                )
            )
        if "screenshot-window" in argv:
            Image.new("RGB", (100, 50), "white").save(
                argv[argv.index("--path") + 1]
            )
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(linux.subprocess, "run", run)
    with pytest.raises(GuiStaleStateError, match="changed during"):
        await linux._capture_niri_window(
            tmp_path / "capture.png", WINDOW, dict(ENV)
        )


@pytest.mark.asyncio
async def test_niri_snapshot_avoids_focus_before_window_capture(
    tmp_path, monkeypatch
):
    from PIL import Image

    backend = linux.LinuxGuiBackend()
    backend._env = {**ENV, "NIRI_SOCKET": "/run/user/1000/niri-test.sock"}
    capture = tmp_path / "capture.png"

    def helper(_payload, _env):
        return {"window": dict(WINDOW), "elements": [], "locators": {}}

    async def native_capture(path, _record, _env):
        Image.new("RGB", (100, 50), "white").save(path)
        return "niri-ipc"

    monkeypatch.setattr(backend, "_helper", helper)
    monkeypatch.setattr(backend, "_list_data", lambda _env: {"monitors": []})
    monkeypatch.setattr(
        backend,
        "_focus_window",
        lambda *_args: pytest.fail("Niri screenshot must not focus"),
    )
    monkeypatch.setattr(linux, "_capture_niri_window", native_capture)

    async def no_devices(_env):
        return 0

    monkeypatch.setattr(linux, "portal_available_devices", no_devices)
    result = await backend.snapshot(
        WINDOW["id"],
        screenshot_path=capture,
        include_elements=False,
        max_elements=10,
        max_depth=2,
    )
    assert result.capabilities["window_capture"] is True
    assert result.capabilities["capture_backend"] == "niri-ipc"
