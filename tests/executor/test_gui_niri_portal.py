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
