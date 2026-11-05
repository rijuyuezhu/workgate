"""KWin's window discovery/focus must fail closed without changing other backends."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from workgate.errors import GuiStaleStateError, GuiUnavailableError
from workgate.executor.gui import linux, linux_kwin

KDE = {
    "XDG_SESSION_TYPE": "wayland",
    "XDG_CURRENT_DESKTOP": "KDE",
    "WAYLAND_DISPLAY": "wayland-0",
}
NATIVE_ID = "ad0cfba5-1a91-4c81-a57c-626f31bce2f2"
AT_SPI = {
    "id": "atspi:47:abc",
    "pid": 47,
    "title": "Test dialog",
    "bounds": {"x": 10, "y": 20, "width": 100, "height": 80},
}
KWIN = {
    "id": f"kwin:{NATIVE_ID}",
    "pid": 47,
    "title": "Test dialog",
    "app": "app",
    "bounds": {"x": 30, "y": 40, "width": 100, "height": 80},
    "active": True,
    "minimized": False,
}


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (KDE, True),
        (
            {
                "XDG_CURRENT_DESKTOP": "KDE",
                "DISPLAY": ":0",
                "XDG_SESSION_TYPE": "x11",
            },
            False,
        ),
        (
            {"XDG_CURRENT_DESKTOP": "niri", "WAYLAND_DISPLAY": "wayland-0"},
            False,
        ),
        (
            {
                "XDG_CURRENT_DESKTOP": "KDE",
                "XDG_SESSION_TYPE": "x11",
                "WAYLAND_DISPLAY": "wayland-0",
            },
            False,
        ),
        (
            {"XDG_CURRENT_DESKTOP": "GNOME:KDE", "XDG_SESSION_TYPE": "wayland"},
            True,
        ),
    ],
)
def test_wayland_detection_is_desktop_scoped(env, expected):
    assert linux_kwin.is_kde_wayland(env) is expected


def test_query_windows_validates_pid_dimensions_and_id(monkeypatch):
    rows = [
        {
            "pid": 47,
            "internal_id": "{" + NATIVE_ID + "}",
            "title": "Test dialog",
            "bounds": {"x": 30, "y": 40, "width": 100, "height": 80},
            "active": True,
        },
        {"pid": 0, "bounds": {"x": 1, "y": 1, "width": 10, "height": 20}},
        {"pid": 44, "bounds": {"x": 0, "y": 0, "width": 0, "height": 40}},
    ]
    monkeypatch.setattr(
        linux_kwin, "_script_results", lambda _script, _env: rows
    )
    listed = linux_kwin.query_windows(KDE)
    assert len(listed) == 1
    assert listed[0]["id"] == f"kwin:{NATIVE_ID}"
    assert listed[0]["bounds"] == {"x": 30, "y": 40, "width": 100, "height": 80}


def test_kwin_merge_prefers_unique_atspi_and_keeps_unmatched_native():
    merged = linux_kwin.merge_windows([AT_SPI], [KWIN])
    assert merged[0]["id"] == AT_SPI["id"]
    assert merged[0]["bounds"] == KWIN["bounds"]
    assert linux_kwin.merge_windows([], [KWIN])[0]["id"] == KWIN["id"]
    ambiguous = linux_kwin.merge_windows(
        [AT_SPI, {**AT_SPI, "id": "atspi:47:other"}], [KWIN]
    )
    assert ambiguous[0]["id"] == KWIN["id"]
    second = {
        **KWIN,
        "id": "kwin:second",
        "bounds": {**KWIN["bounds"], "x": 300},
    }
    assert all(
        item["id"] != AT_SPI["id"]
        for item in linux_kwin.merge_windows([AT_SPI], [KWIN, second])
    )


def test_kwin_refresh_requires_unique_live_identity():
    assert linux_kwin.refresh_window(AT_SPI, [KWIN])["bounds"] == KWIN["bounds"]
    with pytest.raises(GuiStaleStateError, match="ambiguous"):
        linux_kwin.refresh_window(AT_SPI, [KWIN, KWIN])
    with pytest.raises(GuiStaleStateError, match="missing"):
        linux_kwin.refresh_window(KWIN, [{**KWIN, "id": "kwin:changed"}])


def test_focus_must_confirm_activation_and_deny_ambiguous_target(monkeypatch):
    states = iter([[{**KWIN, "active": False}], [KWIN]])
    monkeypatch.setattr(linux_kwin, "query_windows", lambda _env: next(states))
    sent = []
    monkeypatch.setattr(
        linux_kwin,
        "_script_results",
        lambda script, _env: sent.append(script) or [{"selected": True}],
    )
    linux_kwin.focus(KWIN, KDE)
    assert len(sent) == 1 and "expectedPid = 47" in sent[0]
    monkeypatch.setattr(linux_kwin, "query_windows", lambda _env: [KWIN])
    with pytest.raises(GuiStaleStateError, match="geometry changed"):
        linux_kwin.focus(AT_SPI, KDE)
    monkeypatch.setattr(linux_kwin, "query_windows", lambda _env: [KWIN, KWIN])
    with pytest.raises(GuiStaleStateError, match="ambiguous"):
        linux_kwin.focus(AT_SPI, KDE)


def test_script_runner_unloads_and_removes_file_even_on_malformed_response(
    monkeypatch,
):
    calls = []
    captured = {}
    monkeypatch.setattr(
        linux_kwin.shutil, "which", lambda tool: "/usr/bin/" + tool
    )
    monkeypatch.setattr(linux_kwin.time, "sleep", lambda _seconds: None)

    def run(argv, **kwargs):
        calls.append(argv)
        if any("loadScript" in part for part in argv):
            path = Path(argv[-2])
            captured["path"] = path
            captured["content"] = path.read_text()
            return SimpleNamespace(returncode=0, stdout="17", stderr="")
        if argv[0].endswith("journalctl"):
            match = re.search(r'var marker = "([^"]+)"', captured["content"])
            assert match is not None
            marker = match.group(1)
            return SimpleNamespace(
                returncode=0,
                stdout=marker + json.dumps({"selected": True}),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(linux_kwin.subprocess, "run", run)
    assert linux_kwin._script_results("console.info(marker + '{}');", KDE) == [
        {"selected": True}
    ]
    assert not captured["path"].exists()
    assert any(any("unloadScript" in part for part in argv) for argv in calls)


@pytest.mark.asyncio
async def test_backend_lists_only_verified_kwin_rows_and_retains_atspi_id(
    monkeypatch,
):
    backend = linux.LinuxGuiBackend()

    async def env():
        return KDE

    async def devices(_env):
        return 3

    monkeypatch.setattr(backend, "_ensure_env", env)
    monkeypatch.setattr(
        backend,
        "_list_data",
        lambda _env: {
            "windows": [AT_SPI],
            "monitors": [{"x": 0, "y": 0, "width": 800, "height": 600}],
        },
    )
    monkeypatch.setattr(
        backend, "_kde_windows", lambda _env: pytest.fail("Must be awaitable")
    )

    async def kwin(_env):
        return [KWIN]

    monkeypatch.setattr(backend, "_kde_windows", kwin)
    monkeypatch.setattr(linux, "portal_available_devices", devices)
    report = await backend.list_windows()
    assert report["windows"][0]["id"] == AT_SPI["id"]
    assert report["windows"][0]["bounds"] == KWIN["bounds"]
    assert report["capabilities"]["coordinate_input"] is True


@pytest.mark.asyncio
async def test_backend_native_kwin_snapshot_skips_missing_atspi(monkeypatch):
    backend = linux.LinuxGuiBackend()

    async def kwin(_env):
        return [KWIN]

    monkeypatch.setattr(backend, "_kde_windows", kwin)
    monkeypatch.setattr(
        backend, "_helper", lambda *_: pytest.fail("AT-SPI must not be called")
    )
    data = await backend._snapshot_data(
        KWIN["id"], KDE, include_elements=True, max_elements=50, max_depth=2
    )
    assert data["window"]["id"] == KWIN["id"]
    assert not data["elements"] and not data["locators"]
    with pytest.raises(GuiStaleStateError, match="disappeared"):
        await backend._snapshot_data(
            "kwin:invalid",
            KDE,
            include_elements=True,
            max_elements=50,
            max_depth=2,
        )


@pytest.mark.asyncio
async def test_backend_corrects_kde_atspi_geometry_and_element_coordinates(
    monkeypatch,
):
    backend = linux.LinuxGuiBackend()
    monkeypatch.setattr(
        backend,
        "_helper",
        lambda *_: {
            "window": AT_SPI,
            "elements": [
                {
                    "id": "e1",
                    "bounds": {"x": 15, "y": 26, "width": 10, "height": 10},
                }
            ],
            "locators": {
                "e1": {
                    "path": [0],
                    "accessible_id": "id",
                    "fingerprint": "hash",
                }
            },
        },
    )

    async def kwin(_env):
        return [KWIN]

    monkeypatch.setattr(backend, "_kde_windows", kwin)
    data = await backend._snapshot_data(
        AT_SPI["id"], KDE, include_elements=True, max_elements=50, max_depth=2
    )
    assert data["window"]["id"] == AT_SPI["id"]
    assert data["window"]["bounds"] == KWIN["bounds"]
    assert data["elements"][0]["bounds"]["x"] == 35
    assert data["elements"][0]["bounds"]["y"] == 46
    assert data["locators"]["e1"]["path"] == [0]


@pytest.mark.asyncio
async def test_kwin_unavailable_keeps_existing_atspi_activation_but_not_native(
    monkeypatch,
):
    backend = linux.LinuxGuiBackend()

    def unavailable(_window, _env):
        raise GuiUnavailableError("KWin scripting unavailable")

    calls = []
    monkeypatch.setattr(linux_kwin, "focus", unavailable)

    async def atspi(window, _env, _deadline):
        calls.append(window["id"])

    monkeypatch.setattr(backend, "_activate_atspi", atspi)
    await backend._activate_kde(AT_SPI, KDE, None)
    assert calls == [AT_SPI["id"]]
    with pytest.raises(GuiUnavailableError, match="KWin scripting unavailable"):
        await backend._activate_kde(KWIN, KDE, None)
    assert calls == [AT_SPI["id"]]


@pytest.mark.asyncio
async def test_kde_capture_requires_current_window_still_active(
    monkeypatch, tmp_path
):
    async def capture(_path, _bounds, _monitors, _env):
        return "xdg-desktop-portal"

    monkeypatch.setattr(linux, "_capture_wayland", capture)
    monkeypatch.setattr(
        linux_kwin, "query_windows", lambda _env: [{**KWIN, "active": False}]
    )
    with pytest.raises(GuiStaleStateError, match="lost focus"):
        await linux._capture_portal_desktop(tmp_path / "foo.png", KWIN, [], KDE)
    monkeypatch.setattr(linux_kwin, "query_windows", lambda _env: [KWIN])
    assert (
        await linux._capture_portal_desktop(tmp_path / "foo.png", KWIN, [], KDE)
        == "xdg-desktop-portal"
    )


@pytest.mark.asyncio
async def test_non_kde_capture_does_not_query_kwin(monkeypatch, tmp_path):
    async def capture(_path, _bounds, _monitors, _env):
        return "xdg-desktop-portal"

    monkeypatch.setattr(linux, "_capture_wayland", capture)
    monkeypatch.setattr(
        linux_kwin,
        "query_windows",
        lambda _env: pytest.fail("must not query KWin"),
    )
    assert (
        await linux._capture_portal_desktop(
            tmp_path / "foo.png",
            AT_SPI,
            [],
            {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "niri"},
        )
        == "xdg-desktop-portal"
    )


def test_kwin_missing_scripting_tool_fails_before_running(monkeypatch):
    monkeypatch.setattr(linux_kwin.shutil, "which", lambda _tool: None)
    with pytest.raises(GuiUnavailableError, match="requires qdbus"):
        linux_kwin._script_results("console.info('unused')", KDE)


def test_kwin_script_start_failure_unloads_and_removes_ephemeral_file(
    monkeypatch,
):
    calls = []
    source_paths = []
    monkeypatch.setattr(
        linux_kwin.shutil, "which", lambda tool: "/usr/bin/" + tool
    )

    def run(argv, **_kwargs):
        calls.append(argv)
        if any("loadScript" in part for part in argv):
            source_paths.append(Path(argv[-2]))
            return SimpleNamespace(returncode=0, stdout="17", stderr="")
        if any("Scripting.start" in part for part in argv):
            return SimpleNamespace(
                returncode=1, stdout="", stderr="unavailable"
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(linux_kwin.subprocess, "run", run)
    with pytest.raises(GuiUnavailableError, match="did not start"):
        linux_kwin._script_results("console.info('unused')", KDE)
    assert source_paths and all(not path.exists() for path in source_paths)
    assert any(any("unloadScript" in part for part in argv) for argv in calls)


def test_invalid_kwin_rows_and_changed_pid_fail_safely(monkeypatch):
    monkeypatch.setattr(
        linux_kwin,
        "_script_results",
        lambda _script, _env: [
            {"pid": "not-an-integer", "bounds": KWIN["bounds"]},
            {
                "pid": 47,
                "internal_id": "invalid-uuid",
                "bounds": KWIN["bounds"],
            },
        ],
    )
    rows = linux_kwin.query_windows(KDE)
    assert len(rows) == 1 and rows[0]["id"] is None
    with pytest.raises(GuiStaleStateError, match="process changed"):
        linux_kwin.refresh_window(KWIN, [{**KWIN, "pid": 48}])


def test_kwin_focus_rejects_unselected_and_never_active_windows(monkeypatch):
    monkeypatch.setattr(linux_kwin, "query_windows", lambda _env: [KWIN])
    monkeypatch.setattr(linux_kwin, "_script_results", lambda _script, _env: [])
    with pytest.raises(GuiStaleStateError, match="uniquely activate"):
        linux_kwin.focus(KWIN, KDE)

    monkeypatch.setattr(
        linux_kwin,
        "query_windows",
        lambda _env: [{**KWIN, "active": False}],
    )
    monkeypatch.setattr(
        linux_kwin,
        "_script_results",
        lambda _script, _env: [{"selected": True}],
    )
    monkeypatch.setattr(linux_kwin.time, "sleep", lambda _seconds: None)
    with pytest.raises(GuiStaleStateError, match="did not become active"):
        linux_kwin.focus(KWIN, KDE)
