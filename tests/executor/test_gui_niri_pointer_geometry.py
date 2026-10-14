"""Niri pointer coordinate resolution is read-only and fails closed."""

from copy import deepcopy

import pytest

from workgate.errors import GuiStaleStateError, GuiUnavailableError
from workgate.executor.gui.linux_niri_pointer import niri_pointer_target

WINDOW = {
    "id": "atspi:47:stable",
    "pid": 47,
    "title": "Test dialog",
    "bounds": {"x": 0, "y": 0, "width": 320, "height": 240},
}
WINDOWS = [
    {
        "id": 901,
        "pid": 47,
        "title": "Test dialog",
        "is_focused": True,
        "workspace_id": 1,
        "layout": {
            "window_size": [320, 240],
            "tile_pos_in_workspace_view": [800.0, 437.0],
            "window_offset_in_tile": [0.0, 0.0],
        },
    }
]
WORKSPACES = [
    {"id": 1, "output": "eDP-1", "is_active": True},
    {"id": 6, "output": "win-panel", "is_active": True},
]
OUTPUTS = {
    "eDP-1": {
        "logical": {
            "x": 0,
            "y": 0,
            "width": 1920,
            "height": 1080,
            "scale": 1.0,
            "transform": "Normal",
        }
    },
    "win-panel": {
        "logical": {
            "x": -1080,
            "y": 0,
            "width": 1080,
            "height": 1920,
            "scale": 1.0,
            "transform": "90",
        }
    },
}


def resolve(
    x=100, y=100, *, window=None, windows=None, workspaces=None, outputs=None
):
    return niri_pointer_target(
        WINDOW if window is None else window,
        x,
        y,
        windows=WINDOWS if windows is None else windows,
        workspaces=WORKSPACES if workspaces is None else workspaces,
        outputs=OUTPUTS if outputs is None else outputs,
    )


def test_niri_output_local_target_uses_compositor_layout_not_atspi_origin():
    assert resolve() == ("eDP-1", 900, 537)
    assert resolve(0, 0) == ("eDP-1", 800, 437)
    assert resolve(319, 239) == ("eDP-1", 1119, 676)


def test_niri_window_offset_is_included():
    windows = deepcopy(WINDOWS)
    windows[0]["layout"]["window_offset_in_tile"] = [10.0, 5.0]
    assert resolve(windows=windows) == ("eDP-1", 910, 542)


def test_multi_output_uses_workspace_output_not_assumed_primary():
    windows = deepcopy(WINDOWS)
    windows[0]["workspace_id"] = 6
    windows[0]["layout"]["tile_pos_in_workspace_view"] = [120.0, 170.0]
    outputs = deepcopy(OUTPUTS)
    outputs["win-panel"]["logical"]["transform"] = "Normal"
    assert resolve(windows=windows, outputs=outputs) == ("win-panel", 220, 270)


@pytest.mark.parametrize(
    "transform", ["90", "180", "270", "flipped", "flipped-90"]
)
def test_rotated_or_flipped_output_is_not_guessed(transform):
    outputs = deepcopy(OUTPUTS)
    outputs["eDP-1"]["logical"]["transform"] = transform
    with pytest.raises(GuiUnavailableError, match="unverified"):
        resolve(outputs=outputs)


def test_scaled_output_is_not_guessed():
    outputs = deepcopy(OUTPUTS)
    outputs["eDP-1"]["logical"]["scale"] = 1.25
    with pytest.raises(GuiUnavailableError, match="unverified"):
        resolve(outputs=outputs)


@pytest.mark.parametrize("x,y", [(-1, 0), (320, 0), (0, -1), (0, 240)])
def test_target_must_be_inside_observed_window(x, y):
    with pytest.raises(ValueError, match="outside"):
        resolve(x, y)


def test_target_must_be_uniquely_focused():
    windows = deepcopy(WINDOWS)
    windows[0]["is_focused"] = False
    with pytest.raises(GuiStaleStateError, match="focused"):
        resolve(windows=windows)
    with pytest.raises(GuiStaleStateError, match="uniquely"):
        resolve(windows=WINDOWS * 2)


def test_hidden_workspace_or_window_is_rejected():
    workspaces = deepcopy(WORKSPACES)
    workspaces[0]["is_active"] = False
    with pytest.raises(GuiStaleStateError, match="active"):
        resolve(workspaces=workspaces)
    windows = deepcopy(WINDOWS)
    windows[0]["layout"]["tile_pos_in_workspace_view"] = None
    with pytest.raises(GuiUnavailableError, match="position"):
        resolve(windows=windows)


def test_niri_and_atspi_window_dimensions_must_agree():
    windows = deepcopy(WINDOWS)
    windows[0]["layout"]["window_size"] = [350, 260]
    with pytest.raises(GuiStaleStateError, match="dimensions"):
        resolve(windows=windows)


def test_output_target_must_be_visible():
    windows = deepcopy(WINDOWS)
    windows[0]["layout"]["tile_pos_in_workspace_view"] = [1850, 1000]
    with pytest.raises(GuiStaleStateError, match="visible"):
        resolve(windows=windows)


@pytest.mark.parametrize(
    "position", [[float("nan"), 1], [float("inf"), 2], ["oops", 0], [0], {}]
)
def test_invalid_compositor_coordinates_are_rejected(position):
    windows = deepcopy(WINDOWS)
    windows[0]["layout"]["tile_pos_in_workspace_view"] = position
    with pytest.raises(GuiUnavailableError):
        resolve(windows=windows)
