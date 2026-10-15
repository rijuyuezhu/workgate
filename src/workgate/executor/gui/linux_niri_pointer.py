"""Fail-closed mapping of Niri window-local pointer targets to output coordinates.

Niri exposes window position in the *output-local logical* workspace view, not
in AT-SPI's often-zero screen bounds. This module does not inject input.
"""

import math
from typing import Any

from ...errors import GuiStaleStateError, GuiUnavailableError


def _pair(value: Any) -> tuple[float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise GuiUnavailableError(
            "Niri did not expose a usable window position"
        )
    if any(
        not isinstance(x, (int, float)) or isinstance(x, bool) for x in value
    ):
        raise GuiUnavailableError("Niri window position is invalid")
    result = float(value[0]), float(value[1])
    if not all(math.isfinite(x) for x in result):
        raise GuiUnavailableError("Niri window position is non-finite")
    return result


def niri_pointer_target(
    window: dict[str, Any],
    x: int,
    y: int,
    *,
    windows: list[dict[str, Any]],
    workspaces: list[dict[str, Any]],
    outputs: dict[str, dict[str, Any]],
) -> tuple[str, int, int]:
    """Resolve output-local *mode-pixel* coordinates for wdotool.

    Niri layouts use logical pixels, but wdotool 0.5.x sends absolute input
    against the output's untransformed pixel mode. All eight transforms were
    verified with a disposable GTK target on Niri 26.04 at 1.25x scale.
    The caller must revalidate the focused target immediately before input.
    """
    bounds = window.get("bounds")
    if not isinstance(bounds, dict):
        raise GuiStaleStateError("Observed window has no bounds")
    try:
        width, height = int(bounds["width"]), int(bounds["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GuiStaleStateError(
            "Observed window dimensions are invalid"
        ) from exc
    if width <= 0 or height <= 0 or not (0 <= x < width and 0 <= y < height):
        raise ValueError("Pointer target is outside the observed window")

    matches = [
        item
        for item in windows
        if isinstance(item, dict)
        and item.get("pid") == window.get("pid")
        and item.get("title") == window.get("title")
    ]
    if len(matches) != 1 or not matches[0].get("is_focused"):
        raise GuiStaleStateError(
            "Niri target must be uniquely matched and focused"
        )
    current = matches[0]
    matches_ws = [
        item
        for item in workspaces
        if isinstance(item, dict)
        and item.get("id") == current.get("workspace_id")
    ]
    if len(matches_ws) != 1 or not matches_ws[0].get("is_active"):
        raise GuiStaleStateError("Niri target workspace is not active")
    output_name = matches_ws[0].get("output")
    if not isinstance(output_name, str):
        raise GuiUnavailableError("Niri target has no output name")
    output = outputs.get(output_name)
    if not isinstance(output, dict):
        raise GuiUnavailableError("Niri target output has no logical geometry")
    logical = output.get("logical")
    if not isinstance(logical, dict):
        raise GuiUnavailableError("Niri target output has no logical geometry")
    scale = logical.get("scale")
    if (
        not isinstance(scale, (int, float))
        or isinstance(scale, bool)
        or not math.isfinite(scale)
        or scale <= 0
    ):
        raise GuiUnavailableError("Niri output scale is invalid")
    try:
        output_width, output_height = (
            int(logical["width"]),
            int(logical["height"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GuiUnavailableError("Niri output dimensions are invalid") from exc
    if output_width <= 0 or output_height <= 0:
        raise GuiUnavailableError("Niri output dimensions are invalid")
    transform = logical.get("transform")
    if transform not in {
        "Normal",
        "90",
        "180",
        "270",
        "Flipped",
        "Flipped90",
        "Flipped180",
        "Flipped270",
    }:
        raise GuiUnavailableError("Niri output transform is unsupported")
    modes = output.get("modes")
    mode_index = output.get("current_mode")
    if (
        not isinstance(modes, list)
        or not isinstance(mode_index, int)
        or isinstance(mode_index, bool)
        or not 0 <= mode_index < len(modes)
    ):
        raise GuiUnavailableError("Niri output has no current pixel mode")
    mode = modes[mode_index]
    if not isinstance(mode, dict):
        raise GuiUnavailableError("Niri output has no current pixel mode")
    try:
        mode_width = int(mode["width"])
        mode_height = int(mode["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GuiUnavailableError("Niri pixel mode is invalid") from exc
    expected_width, expected_height = (
        (output_height, output_width)
        if transform in {"90", "270", "Flipped90", "Flipped270"}
        else (output_width, output_height)
    )
    if (
        mode_width <= 0
        or mode_height <= 0
        or abs(mode_width - expected_width * scale) > 2
        or abs(mode_height - expected_height * scale) > 2
    ):
        raise GuiUnavailableError(
            "Niri logical and pixel output sizes disagree"
        )
    layout = current.get("layout")
    if not isinstance(layout, dict):
        raise GuiUnavailableError("Niri target has no compositor layout")
    window_width, window_height = _pair(layout.get("window_size"))
    if abs(window_width - width) > 1 or abs(window_height - height) > 1:
        raise GuiStaleStateError("Niri and AT-SPI window dimensions differ")
    left, top = _pair(layout.get("tile_pos_in_workspace_view"))
    offset_x, offset_y = _pair(layout.get("window_offset_in_tile"))
    px, py = left + offset_x + x, top + offset_y + y
    if not (0 <= px < output_width and 0 <= py < output_height):
        raise GuiStaleStateError(
            "Pointer target is outside the visible Niri output"
        )
    # Inverse wl_output transform, from logical to untransformed mode pixels.
    # wdotool's --output creates a per-output virtual pointer but its absolute
    # motion extent is the output's *physical mode*, not its logical size.
    if transform == "Normal":
        raw_x, raw_y = px, py
    elif transform == "90":
        raw_x, raw_y = py, output_width - px
    elif transform == "180":
        raw_x, raw_y = output_width - px, output_height - py
    elif transform == "270":
        raw_x, raw_y = output_height - py, px
    elif transform == "Flipped":
        raw_x, raw_y = output_width - px, py
    elif transform == "Flipped90":
        raw_x, raw_y = py, px
    elif transform == "Flipped180":
        raw_x, raw_y = px, output_height - py
    else:  # Flipped270
        raw_x, raw_y = output_height - py, output_width - px
    return (
        output_name,
        min(mode_width - 1, max(0, round(raw_x * scale))),
        min(mode_height - 1, max(0, round(raw_y * scale))),
    )
