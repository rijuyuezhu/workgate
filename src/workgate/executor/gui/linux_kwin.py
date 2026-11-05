"""KWin Wayland window inventory and focus through the desktop user's D-Bus session."""

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from string import Template
from typing import Any

from ...errors import GuiStaleStateError, GuiUnavailableError
from .base import (
    GUI_MAX_CAPTURE_DIMENSION,
    GUI_MAX_CAPTURE_PIXELS,
    GUI_MAX_WINDOW_TEXT_BYTES,
    GUI_MAX_WINDOWS,
)


def is_kde_wayland(env: dict[str, str]) -> bool:
    desktop = str(env.get("XDG_CURRENT_DESKTOP") or "").upper()
    session = str(env.get("XDG_SESSION_TYPE") or "").lower()
    wayland = session == "wayland" or (
        session != "x11" and bool(env.get("WAYLAND_DISPLAY"))
    )
    return wayland and "KDE" in desktop


def _bounds(value: Any) -> tuple[int, int, int, int] | None:
    try:
        x, y, width, height = (
            int(value[key]) for key in ("x", "y", "width", "height")
        )
    except KeyError, TypeError, ValueError:
        return None
    if (
        width <= 0
        or height <= 0
        or width > GUI_MAX_CAPTURE_DIMENSION
        or height > GUI_MAX_CAPTURE_DIMENSION
        or width * height > GUI_MAX_CAPTURE_PIXELS
    ):
        return None
    return x, y, width, height


def _kwin_id(internal_id: Any) -> str | None:
    try:
        return f"kwin:{uuid.UUID(str(internal_id).strip('{}'))}"
    except ValueError, TypeError, AttributeError:
        return None


def _script_results(script: str, env: dict[str, str]) -> list[dict[str, Any]]:
    """Run one fixed KWin script and read its bounded, uniquely marked journal output."""
    qdbus = shutil.which("qdbus6") or shutil.which("qdbus")
    journalctl = shutil.which("journalctl")
    if not qdbus or not journalctl:
        raise GuiUnavailableError(
            "KWin scripting requires qdbus6/qdbus and journalctl"
        )
    marker = f"WORKGATE_KWIN_{uuid.uuid4().hex}:"
    name = f"workgate-{uuid.uuid4().hex}"
    fd, raw_path = tempfile.mkstemp(prefix="workgate-kwin-", suffix=".js")
    path = Path(raw_path)
    loaded = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(f"var marker = {json.dumps(marker)};\n" + script)
        result = subprocess.run(
            [
                qdbus,
                "org.kde.KWin",
                "/Scripting",
                "org.kde.kwin.Scripting.loadScript",
                str(path),
                name,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
        if result.returncode or not result.stdout.strip().isdigit():
            raise GuiUnavailableError("KWin did not accept the window script")
        loaded = True
        started = subprocess.run(
            [
                qdbus,
                "org.kde.KWin",
                "/Scripting",
                "org.kde.kwin.Scripting.start",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
        if started.returncode:
            raise GuiUnavailableError("KWin did not start the window script")
        time.sleep(0.08)
        logs = subprocess.run(
            [journalctl, "--user", "-n", "400", "--no-pager", "-o", "cat"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
        if logs.returncode:
            raise GuiUnavailableError("Could not read KWin window results")
        rows = []
        for line in logs.stdout.splitlines():
            if marker not in line:
                continue
            payload = line.split(marker, 1)[1]
            if len(payload.encode("utf-8")) > 4096:
                continue
            try:
                item = json.loads(payload)
            except ValueError, TypeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
            if len(rows) >= GUI_MAX_WINDOWS * 4:
                break
        return rows
    except (OSError, subprocess.SubprocessError) as exc:
        raise GuiUnavailableError("KWin scripting request failed") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if loaded:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    [
                        qdbus,
                        "org.kde.KWin",
                        "/Scripting",
                        "org.kde.kwin.Scripting.unloadScript",
                        name,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                    env=env,
                )
        path.unlink(missing_ok=True)


_WINDOW_SCRIPT = r"""
var windows = workspace.stackingOrder ||
    (typeof workspace.windowList === "function" ? workspace.windowList() :
    (typeof workspace.clientList === "function" ? workspace.clientList() : []));
var active = ("activeWindow" in workspace) ? workspace.activeWindow : workspace.activeClient;
for (var i = windows.length - 1; i >= 0; --i) {
    var w = windows[i];
    if (!(w.normalWindow || w.dialog || w.utility)) continue;
    var g = w.clientGeometry || w.frameGeometry;
    if (!g) continue;
    console.info(marker + JSON.stringify({
        pid: Number(w.pid), title: String(w.caption || ""),
        internal_id: String(w.internalId || ""),
        app: String(w.desktopFileName || w.resourceClass || w.resourceName || ""),
        minimized: Boolean(w.minimized), active: w === active,
        bounds: {x: Math.round(g.x), y: Math.round(g.y),
                 width: Math.round(g.width), height: Math.round(g.height)}
    }));
}
"""


def query_windows(env: dict[str, str]) -> list[dict[str, Any]]:
    records = []
    for item in _script_results(_WINDOW_SCRIPT, env):
        try:
            pid = int(item.get("pid") or 0)
            bounds = _bounds(item.get("bounds"))
        except TypeError, ValueError:
            continue
        if pid <= 0 or bounds is None:
            continue
        records.append(
            {
                "id": _kwin_id(item.get("internal_id")),
                "pid": pid,
                "title": str(item.get("title") or "")[
                    :GUI_MAX_WINDOW_TEXT_BYTES
                ],
                "app": str(item.get("app") or "")[:GUI_MAX_WINDOW_TEXT_BYTES],
                "bounds": dict(
                    zip(("x", "y", "width", "height"), bounds, strict=True)
                ),
                "minimized": bool(item.get("minimized")),
                "active": bool(item.get("active")),
            }
        )
        if len(records) >= GUI_MAX_WINDOWS:
            break
    return records


def _matches(atspi: dict[str, Any], kwin: dict[str, Any]) -> bool:
    if str(atspi.get("pid")) != str(kwin.get("pid")):
        return False
    title = str(atspi.get("title") or "")
    if title and title != str(kwin.get("title") or ""):
        return False
    left = _bounds(atspi.get("bounds"))
    right = _bounds(kwin.get("bounds"))
    return bool(
        left
        and right
        and abs(left[2] - right[2]) <= 8
        and abs(left[3] - right[3]) <= 8
    )


def _matched_atspi(
    kwin: dict[str, Any],
    atspi: list[dict[str, Any]],
    used: set[str],
    all_kwin: list[dict[str, Any]],
) -> dict[str, Any] | None:
    matches = [
        row
        for row in atspi
        if str(row.get("id") or "") not in used and _matches(row, kwin)
    ]
    if len(matches) != 1:
        return None
    # Two KWin windows with the same PID/title/size must not compete for one
    # AT-SPI identity; prefer native UUIDs over guessing a clickable target.
    if sum(_matches(matches[0], row) for row in all_kwin) != 1:
        return None
    return matches[0]


def merge_windows(
    atspi: list[dict[str, Any]], kwin: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Prefer AT-SPI semantic IDs; use native KWin IDs for unmatched windows."""
    used: set[str] = set()
    result = []
    for native in kwin:
        observed = _matched_atspi(native, atspi, used, kwin)
        if observed is not None:
            used.add(str(observed["id"]))
            result.append(
                {**observed, **{k: v for k, v in native.items() if k != "id"}}
            )
        elif native.get("id"):
            result.append(dict(native))
        if len(result) >= GUI_MAX_WINDOWS:
            break
    return result


def refresh_window(
    window: dict[str, Any], kwin: list[dict[str, Any]]
) -> dict[str, Any]:
    """Require one still-identifiable KWin window before control or capture."""
    window_id = str(window.get("id") or "")
    if window_id.startswith("kwin:"):
        rows = [item for item in kwin if item.get("id") == window_id]
    else:
        rows = [item for item in kwin if _matches(window, item)]
    if len(rows) != 1:
        raise GuiStaleStateError(
            "KWin target is missing or ambiguous; refresh gui_state"
        )
    current = rows[0]
    if str(window.get("pid")) != str(current["pid"]):
        raise GuiStaleStateError(
            "KWin target process changed; refresh gui_state"
        )
    return {**window, **{k: v for k, v in current.items() if k != "id"}}


def focus(window: dict[str, Any], env: dict[str, str]) -> None:
    """Activate only a uniquely identified KWin window and check its active state."""
    current = refresh_window(window, query_windows(env))
    observed_bounds = _bounds(window.get("bounds"))
    current_bounds = _bounds(current.get("bounds"))
    if observed_bounds != current_bounds:
        raise GuiStaleStateError(
            "KWin target geometry changed; refresh gui_state"
        )
    bounds = current_bounds
    if bounds is None:
        raise GuiStaleStateError("KWin target has invalid geometry")
    pid = int(current["pid"])
    title = str(current.get("title") or "")
    identity = str(window.get("id") or "")
    native_id = (
        identity.removeprefix("kwin:") if identity.startswith("kwin:") else ""
    )
    script = Template("""var expectedPid = $pid;
var expectedTitle = $title;
var expectedId = $id;
var expected = $bounds;
var windows = workspace.stackingOrder ||
    (typeof workspace.windowList === "function" ? workspace.windowList() :
    (typeof workspace.clientList === "function" ? workspace.clientList() : []));
var matches = [];
for (var i = 0; i < windows.length; ++i) {
    var w = windows[i];
    var g = w.clientGeometry || w.frameGeometry;
    if (!g || Number(w.pid) !== expectedPid) continue;
    if (expectedId && String(w.internalId || "").replace(/[{}]/g, "").toLowerCase() !== expectedId) continue;
    if (!expectedId && String(w.caption || "") !== expectedTitle) continue;
    if (Math.abs(g.x - expected.x) > 16 || Math.abs(g.y - expected.y) > 16 ||
        Math.abs(g.width - expected.width) > 64 || Math.abs(g.height - expected.height) > 64) continue;
    matches.push(w);
}
if (matches.length === 1) {
    if (typeof workspace.raiseWindow === "function") workspace.raiseWindow(matches[0]);
    else if (typeof workspace.raiseClient === "function") workspace.raiseClient(matches[0]);
    if ("activeWindow" in workspace) workspace.activeWindow = matches[0];
    else workspace.activeClient = matches[0];
}
console.info(marker + JSON.stringify({selected: matches.length === 1}));
""").substitute(
        pid=pid,
        title=json.dumps(title),
        id=json.dumps(native_id),
        bounds=json.dumps(
            dict(zip(("x", "y", "width", "height"), bounds, strict=True))
        ),
    )
    result = _script_results(script, env)
    if len(result) != 1 or result[0].get("selected") is not True:
        raise GuiStaleStateError("KWin could not uniquely activate the target")
    for _ in range(3):
        active = refresh_window(window, query_windows(env))
        if active.get("active"):
            return
        time.sleep(0.08)
    raise GuiStaleStateError("KWin target did not become active")
