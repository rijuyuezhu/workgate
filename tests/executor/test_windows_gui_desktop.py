"""Windows desktop eligibility and foreground input security contracts."""

from types import SimpleNamespace

import pytest

from tests.executor.test_windows_capture import NativeCall
from workgate.errors import GuiUnavailableError
from workgate.executor.gui import windows
from workgate.executor.gui.windows import WindowsGuiBackend


@pytest.fixture
def desktops(monkeypatch):
    state = {
        "session": 1,
        "desktop": "Default",
        "foreground": 42,
        "open": True,
        "session_lookup": True,
        "info_lookup": True,
    }

    def process_session(_pid, target):
        target._obj.value = state["session"]
        return int(state["session_lookup"])

    def get_name(_desktop, _field, buffer, _capacity, _used):
        buffer.value = state["desktop"]
        return int(state["info_lookup"])

    user = SimpleNamespace(
        OpenInputDesktop=NativeCall(lambda *_args: 123 if state["open"] else 0),
        CloseDesktop=NativeCall(1),
        GetUserObjectInformationW=NativeCall(get_name),
        GetForegroundWindow=NativeCall(lambda: state["foreground"]),
    )
    kernel = SimpleNamespace(ProcessIdToSessionId=NativeCall(process_session))
    monkeypatch.setattr(
        windows, "os", SimpleNamespace(name="nt", getpid=lambda: 42)
    )
    monkeypatch.setattr(windows.sys, "platform", "win32")
    monkeypatch.setattr(
        windows.ctypes,
        "WinDLL",
        lambda name, **kwargs: user if name == "user32" else kernel,
        raising=False,
    )
    monkeypatch.setattr(
        windows.ctypes, "windll", SimpleNamespace(user32=user), raising=False
    )
    return state, user


def test_windows_gui_accepts_only_unlocked_default_desktop(desktops):
    state, user = desktops
    assert windows.windows_desktop_available()
    assert len(user.CloseDesktop.calls) == 1

    state["foreground"] = 0
    assert not windows.windows_desktop_available()
    state["foreground"] = 42
    state["desktop"] = "Winlogon"
    assert not windows.windows_desktop_available()
    state["desktop"] = "Default"
    state["info_lookup"] = False
    assert not windows.windows_desktop_available()
    state["info_lookup"] = True
    state["open"] = False
    assert not windows.windows_desktop_available()
    state["open"] = True
    state["session"] = 0
    assert not windows.windows_desktop_available()
    state["session"] = 1
    state["session_lookup"] = False
    assert not windows.windows_desktop_available()


def test_windows_gui_enforces_foreground_ownership(desktops):
    state, _user = desktops
    windows._require_foreground(42)
    with pytest.raises(GuiUnavailableError, match="foreground window"):
        windows._require_foreground(100)
    state["foreground"] = 0
    with pytest.raises(GuiUnavailableError, match="foreground window"):
        windows._require_foreground(42)


@pytest.mark.asyncio
async def test_windows_gui_refuses_headless_provider_call(desktops):
    state, _user = desktops
    state["foreground"] = 0
    backend = WindowsGuiBackend()
    try:
        with pytest.raises(GuiUnavailableError, match="unlocked interactive"):
            await backend.list_windows()
    finally:
        await backend.aclose()
