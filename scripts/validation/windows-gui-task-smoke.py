"""Disposable native Windows GUI smoke run from Task Scheduler's logged-in user."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("Run with Windows Python")

from workgate.platform.windows_task import WindowsUserTask


def _run(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=35)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return result


def _desktop_probe(output: Path) -> None:
    """Create an isolated Tk window, inspect only that window, and dispose it."""
    import tkinter

    from workgate.executor.gui.windows import (
        WindowsGuiBackend,
        windows_desktop_available,
    )

    outcome = {"interactive": windows_desktop_available()}
    import ctypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.OpenInputDesktop.argtypes = [
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    user.OpenInputDesktop.restype = ctypes.c_void_p
    handle = user.OpenInputDesktop(0, False, 0x101)
    if handle:
        name = ctypes.create_unicode_buffer(256)
        needed = ctypes.c_ulong()
        user.GetUserObjectInformationW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
        ]
        user.GetUserObjectInformationW.restype = ctypes.c_int
        ok = user.GetUserObjectInformationW(
            handle, 2, name, ctypes.sizeof(name), ctypes.byref(needed)
        )
        outcome["input_desktop_name"] = name.value if ok else "(unknown)"
        user.CloseDesktop.argtypes = [ctypes.c_void_p]
        user.CloseDesktop.restype = ctypes.c_int
        user.CloseDesktop(handle)
    root = None
    backend = WindowsGuiBackend()
    try:
        if not outcome["interactive"]:
            outcome["skipped"] = (
                "Windows input desktop is unavailable or locked"
            )
            return
        root = tkinter.Tk()
        root.title("Workgate 389 disposable GUI smoke")
        root.geometry("300x170+25+25")
        tkinter.Label(root, text="Workgate GUI smoke").pack()
        button_clicks = [0]
        button = tkinter.Button(
            root,
            text="Test button",
            command=lambda: button_clicks.__setitem__(0, button_clicks[0] + 1),
        )
        button.pack()
        entry = tkinter.Entry(root)
        entry.pack()
        root.update()
        root.lift()
        root.focus_force()
        root.update()
        entry_x, entry_y = entry.winfo_rootx(), entry.winfo_rooty()
        button_x, button_y = button.winfo_rootx(), button.winfo_rooty()

        async def inspect():
            listing = await backend.list_windows()
            matches = [
                window
                for window in listing["windows"]
                if window.get("title") == "Workgate 389 disposable GUI smoke"
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    f"Expected disposable window, got {len(matches)}"
                )
            capture = output.with_suffix(".png")
            snap = await backend.snapshot(
                matches[0]["id"],
                screenshot_path=capture,
                include_elements=True,
                max_elements=20,
                max_depth=4,
            )
            outcome["window_geometry"] = snap.window["bounds"]
            outcome["elements"] = len(snap.elements)
            outcome["captured"] = (
                capture.is_file() and capture.stat().st_size > 100
            )
            capture.unlink(missing_ok=True)
            await backend.focus_window(snap.window)
            origin = snap.window["bounds"]
            await backend.perform_action(
                snap.window,
                None,
                {
                    "type": "click",
                    "x": entry_x - origin["x"] + 12,
                    "y": entry_y - origin["y"] + 8,
                },
            )
            await backend.perform_action(
                snap.window, None, {"type": "type", "text": "native-ui"}
            )
            # SendInput is queued on the desktop: allow Tk's event loop to
            # process the typed keys before issuing Backspace.
            await asyncio.sleep(0.15)
            await backend.perform_action(
                snap.window, None, {"type": "key", "keys": "Backspace"}
            )
            await backend.perform_action(
                snap.window,
                None,
                {"type": "scroll", "x": 10, "y": 10, "delta_y": -1},
            )
            await backend.perform_action(
                snap.window,
                None,
                {
                    "type": "click",
                    "x": button_x - origin["x"] + 12,
                    "y": button_y - origin["y"] + 8,
                },
            )
            return outcome

        result = {}

        def inspect_on_worker():
            try:
                result.update(asyncio.run(inspect()))
            except BaseException as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=inspect_on_worker)
        thread.start()
        while thread.is_alive():
            root.update()
            time.sleep(0.01)
        thread.join()
        outcome.update(result)
        for _ in range(30):
            root.update()
            time.sleep(0.01)
        outcome["typed"] = entry.get() == "native-u"
        outcome["button_clicked"] = button_clicks[0] == 1
        outcome["entry_length"] = len(entry.get())
        outcome["clicked_count"] = button_clicks[0]
        outcome["tk_focus"] = str(root.focus_get()) == str(entry)
        import ctypes

        user32 = ctypes.windll.user32
        outcome["foreground_is_tk"] = bool(
            user32.GetForegroundWindow()
            == user32.GetAncestor(root.winfo_id(), 2)
        )
        outcome["pass"] = bool(
            outcome.get("captured")
            and outcome["typed"]
            and outcome["button_clicked"]
        )
        if outcome.get("captured") and (
            str(outcome.get("error", "")).startswith(
                "GuiUnavailableError: Windows target is not the foreground window"
            )
        ):
            outcome["skipped"] = (
                "the login session cannot take keyboard/mouse focus"
            )
    except BaseException as exc:
        outcome["pass"] = False
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        asyncio.run(backend.aclose())
        if root is not None:
            root.destroy()
        output.write_text(
            json.dumps(outcome, ensure_ascii=False), encoding="utf-8"
        )


def _assert_headless() -> None:
    """Native no-desktop contract: GUI is unavailable and never enumerates UIA."""
    from workgate.errors import GuiUnavailableError
    from workgate.executor.gui import gui_capability_available
    from workgate.executor.gui.windows import (
        WindowsGuiBackend,
        windows_desktop_available,
    )

    if windows_desktop_available():
        raise RuntimeError(
            "Headless acceptance requires a noninteractive Windows runner"
        )
    if gui_capability_available():
        raise AssertionError("Headless Executor incorrectly advertises GUI")

    async def probe() -> None:
        backend = WindowsGuiBackend()
        try:
            try:
                await backend.list_windows()
            except GuiUnavailableError as exc:
                if "unlocked interactive input desktop" not in str(exc):
                    raise
            else:
                raise AssertionError(
                    "Headless Executor enumerated desktop windows"
                )
        finally:
            await backend.aclose()

    asyncio.run(probe())
    print("Native Windows headless GUI unavailable and fail-closed: PASS")


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "--assert-headless":
        _assert_headless()
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--probe":
        _desktop_probe(Path(sys.argv[2]))
        return
    strict_ci = len(sys.argv) == 2 and sys.argv[1] == "--ci"
    if len(sys.argv) != 1 and not strict_ci:
        raise SystemExit("optional --ci for strict native desktop acceptance")
    if strict_ci:
        from workgate.executor.gui.windows import windows_desktop_available

        if not windows_desktop_available():
            _assert_headless()
            return
    root = Path(tempfile.gettempdir()) / f"workgate389-{os.getpid()}"
    root.mkdir()
    output = root / "result.json"
    task = WindowsUserTask(
        f"Workgate-389-Smoke-{os.getpid()}",
        description="Managed by Workgate: disposable Windows GUI smoke",
        run=_run,
    )
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    try:
        task.install(
            [
                str(pythonw),
                str(Path(__file__).resolve()),
                "--probe",
                str(output),
            ],
            cwd=str(root),
        )
        task.start()
        for _ in range(120):
            if output.is_file():
                break
            time.sleep(0.25)
        if not output.is_file():
            raise RuntimeError(
                f"No interactive smoke result, task={task.status()}"
            )
        result = json.loads(output.read_text(encoding="utf-8"))
        print(json.dumps(result, ensure_ascii=False))
        if not result.get("pass") and (strict_ci or not result.get("skipped")):
            raise SystemExit(1)
    finally:
        task.uninstall()
        if output.exists():
            output.unlink()
        root.rmdir()


if __name__ == "__main__":
    main()
