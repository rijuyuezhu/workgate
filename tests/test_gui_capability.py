import asyncio
from types import SimpleNamespace

import workgate.executor.gui as gui


def test_gui_capability_is_absent_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(gui.platform, "system", lambda: "Windows")
    assert gui.gui_capability_available() is False


def test_gui_capability_is_absent_on_headless_linux(monkeypatch) -> None:
    monkeypatch.setattr(gui.platform, "system", lambda: "Linux")
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        gui,
        "_linux_desktop_environment_fallback",
        lambda: (None, None, None),
    )
    assert gui.gui_capability_available() is False


def test_gui_capability_uses_linux_user_environment_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setattr(gui.platform, "system", lambda: "Linux")
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        gui,
        "_linux_desktop_environment_fallback",
        lambda: (":9", None, "x11"),
    )
    monkeypatch.setattr(
        gui,
        "_linux_gui_capability_available",
        lambda display, wayland, session_type: (
            (
                display,
                wayland,
                session_type,
            )
            == (":9", None, "x11")
        ),
    )
    assert gui.gui_capability_available() is True


def test_gui_capability_fills_missing_explicit_linux_transport(
    monkeypatch,
) -> None:
    monkeypatch.setattr(gui.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(
        gui,
        "_linux_desktop_environment_fallback",
        lambda: (":9", None, "x11"),
    )
    monkeypatch.setattr(
        gui,
        "_linux_gui_capability_available",
        lambda display, wayland, session_type: (
            (display, wayland, session_type) == (":9", None, "x11")
        ),
    )
    assert gui.gui_capability_available() is True


def test_linux_gui_capability_rejects_session_type_without_transport(
    monkeypatch,
) -> None:
    gui._linux_gui_capability_available.cache_clear()
    monkeypatch.setattr(gui.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(
        gui.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(gui.importlib.util, "find_spec", lambda _name: object())

    assert gui._linux_gui_capability_available(None, None, "x11") is False
    gui._linux_gui_capability_available.cache_clear()
    assert gui._linux_gui_capability_available(None, None, "wayland") is False


def test_linux_gui_capability_requires_atspi_and_transport_binding(
    monkeypatch,
) -> None:
    gui._linux_gui_capability_available.cache_clear()
    monkeypatch.setattr(gui.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(
        gui.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        gui.importlib.util,
        "find_spec",
        lambda name: object() if name == "Xlib" else None,
    )

    assert gui._linux_gui_capability_available(":0", None, "x11") is True

    gui._linux_gui_capability_available.cache_clear()
    monkeypatch.setattr(gui.importlib.util, "find_spec", lambda _name: None)
    assert gui._linux_gui_capability_available(":0", None, "x11") is False


def test_macos_gui_capability_requires_native_bindings_not_granted_permissions(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        gui.importlib.util,
        "find_spec",
        lambda _name: object(),
    )
    assert gui._macos_gui_capability_available() is True

    monkeypatch.setattr(
        gui.importlib.util,
        "find_spec",
        lambda name: None if name == "Quartz" else object(),
    )
    assert gui._macos_gui_capability_available() is False


def test_gui_backend_factory_selects_supported_platforms(monkeypatch) -> None:
    monkeypatch.setattr(gui.platform, "system", lambda: "Linux")
    linux_backend = gui._backend_for_platform()
    assert linux_backend.name == "linux-atspi"

    monkeypatch.setattr(gui.platform, "system", lambda: "Darwin")
    mac_backend = gui._backend_for_platform()
    assert mac_backend.name == "macos-ax"
    asyncio.run(mac_backend.aclose())


def test_gui_backend_factory_rejects_unsupported_platform(monkeypatch) -> None:
    monkeypatch.setattr(gui.platform, "system", lambda: "")
    monkeypatch.setattr(gui.platform, "platform", lambda: "mystery-os")
    try:
        gui._backend_for_platform()
    except RuntimeError as exc:
        assert "mystery-os" in str(exc)
    else:
        raise AssertionError("unsupported GUI platform unexpectedly accepted")


def test_linux_gui_capability_handles_candidate_and_wayland_edges(
    monkeypatch,
) -> None:
    gui._linux_gui_capability_available.cache_clear()
    monkeypatch.setattr(gui.shutil, "which", lambda _name: None)
    monkeypatch.setattr(gui.os.path, "isfile", lambda _path: False)
    assert gui._linux_gui_capability_available(":0", None, "x11") is False

    gui._linux_gui_capability_available.cache_clear()
    monkeypatch.setattr(gui.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(
        gui.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no python")),
    )
    assert gui._linux_gui_capability_available(":0", None, "x11") is False

    gui._linux_gui_capability_available.cache_clear()
    monkeypatch.setattr(
        gui.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )
    assert gui._linux_gui_capability_available(":0", None, "x11") is False

    gui._linux_gui_capability_available.cache_clear()
    monkeypatch.setattr(
        gui.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        gui.importlib.util,
        "find_spec",
        lambda name: object() if name == "dbus_next" else None,
    )
    assert gui._linux_gui_capability_available(None, "wayland-0", None) is True

    gui._linux_gui_capability_available.cache_clear()
    assert gui._linux_gui_capability_available(":0", None, "mir") is False


def test_gui_capability_direct_platform_paths(monkeypatch) -> None:
    monkeypatch.setattr(gui.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(gui, "_macos_gui_capability_available", lambda: True)
    assert gui.gui_capability_available() is True

    monkeypatch.setattr(gui.platform, "system", lambda: "Linux")
    monkeypatch.setenv("DISPLAY", ":5")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(
        gui,
        "_linux_gui_capability_available",
        lambda display, wayland, session_type: (
            (
                display,
                wayland,
                session_type,
            )
            == (":5", None, "x11")
        ),
    )
    monkeypatch.setattr(
        gui,
        "_linux_desktop_environment_fallback",
        lambda: (_ for _ in ()).throw(AssertionError("fallback not expected")),
    )
    assert gui.gui_capability_available() is True
