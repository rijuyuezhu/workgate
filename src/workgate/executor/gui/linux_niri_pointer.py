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
    """Resolve output and local logical coordinates; never guess unsafe layouts.

    Rotation and non-1.0 scaling need real device verification before use.
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
    logical = output.get("logical") if isinstance(output, dict) else None
    if not isinstance(logical, dict):
        raise GuiUnavailableError("Niri target output has no logical geometry")
    if (
        logical.get("transform") not in {"Normal", "normal"}
        or logical.get("scale") != 1
    ):
        raise GuiUnavailableError(
            "Niri rotated or scaled output pointer mapping is unverified"
        )
    try:
        output_width, output_height = (
            int(logical["width"]),
            int(logical["height"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GuiUnavailableError("Niri output dimensions are invalid") from exc
    layout = current.get("layout")
    if not isinstance(layout, dict):
        raise GuiUnavailableError("Niri target has no compositor layout")
    window_width, window_height = _pair(layout.get("window_size"))
    if abs(window_width - width) > 1 or abs(window_height - height) > 1:
        raise GuiStaleStateError("Niri and AT-SPI window dimensions differ")
    left, top = _pair(layout.get("tile_pos_in_workspace_view"))
    offset_x, offset_y = _pair(layout.get("window_offset_in_tile"))
    px, py = round(left + offset_x + x), round(top + offset_y + y)
    if not (0 <= px < output_width and 0 <= py < output_height):
        raise GuiStaleStateError(
            "Pointer target is outside the visible Niri output"
        )
    return output_name, px, py
