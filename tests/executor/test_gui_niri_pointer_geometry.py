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
        },
        "modes": [{"width": 1920, "height": 1080}],
        "current_mode": 0,
    },
    "win-panel": {
        "logical": {
            "x": -1080,
            "y": 0,
            "width": 1080,
            "height": 1920,
            "scale": 1.0,
            "transform": "90",
        },
        "modes": [{"width": 1920, "height": 1080}],
        "current_mode": 0,
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
    outputs["win-panel"]["logical"].update(
        {"transform": "Normal", "width": 1080, "height": 1920}
    )
    outputs["win-panel"]["modes"][0].update({"width": 1080, "height": 1920})
    assert resolve(windows=windows, outputs=outputs) == ("win-panel", 220, 270)


@pytest.mark.parametrize(
    ("transform", "expected"),
    [
        ("Normal", (250, 250)),
        ("90", (250, 710)),
        ("180", (1030, 710)),
        ("270", (1030, 250)),
        ("Flipped", (1030, 250)),
        ("Flipped90", (250, 250)),
        ("Flipped180", (250, 710)),
        ("Flipped270", (1030, 710)),
    ],
)
def test_all_niri_transforms_at_fractional_scale(transform, expected):
    outputs = deepcopy(OUTPUTS)
    logical = outputs["eDP-1"]["logical"]
    logical.update({"scale": 1.25, "transform": transform})
    logical["width"], logical["height"] = (
        (768, 1024)
        if transform in {"90", "270", "Flipped90", "Flipped270"}
        else (1024, 768)
    )
    outputs["eDP-1"]["modes"][0].update({"width": 1280, "height": 960})
    windows = deepcopy(WINDOWS)
    windows[0]["layout"]["tile_pos_in_workspace_view"] = [100, 100]
    assert resolve(windows=windows, outputs=outputs) == ("eDP-1", *expected)


@pytest.mark.parametrize("scale", [0, -1, float("nan"), float("inf"), True])
def test_invalid_scale_fails_closed(scale):
    outputs = deepcopy(OUTPUTS)
    outputs["eDP-1"]["logical"]["scale"] = scale
    with pytest.raises(GuiUnavailableError, match="scale"):
        resolve(outputs=outputs)


@pytest.mark.parametrize("mode", [None, [], [{"width": 1111, "height": 1080}]])
def test_missing_or_disagreeing_pixel_mode_fails_closed(mode):
    outputs = deepcopy(OUTPUTS)
    outputs["eDP-1"]["modes"] = mode
    with pytest.raises(GuiUnavailableError, match="mode|disagree"):
        resolve(outputs=outputs)


def test_unknown_transform_fails_closed():
    outputs = deepcopy(OUTPUTS)
    outputs["eDP-1"]["logical"]["transform"] = "unsupported"
    with pytest.raises(GuiUnavailableError, match="transform"):
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
