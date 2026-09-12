from io import BytesIO
from typing import Any

import pytest
from PIL import Image

import workgate.utils.image_preview as preview_module


class _OversizedImage:
    size = (5_001, 5_000)

    def __enter__(self) -> _OversizedImage:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def seek(self, frame: int) -> None:
        assert frame == 0


def test_source_pixel_limit_is_checked_before_orientation_or_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        preview_module.Image,
        "open",
        lambda _source: _OversizedImage(),
    )

    def fail_transpose(_image: Any) -> Any:
        raise AssertionError("oversized image must be rejected before decoding")

    monkeypatch.setattr(
        preview_module.ImageOps, "exif_transpose", fail_transpose
    )

    with pytest.raises(ValueError, match="25005000 pixels"):
        preview_module.make_image_preview(b"header", 80, 24)


@pytest.mark.parametrize(
    ("size", "expected_cells"),
    [((5, 3), (3, 2)), ((6, 4), (3, 2)), ((1, 1), (2, 1))],
)
def test_supersampled_pixels_map_to_terminal_cells_with_ceiling_division(
    size: tuple[int, int], expected_cells: tuple[int, int]
) -> None:
    buffer = BytesIO()
    Image.new("RGBA", size, (10, 20, 30, 255)).save(buffer, format="PNG")

    preview = preview_module.make_image_preview(
        buffer.getvalue(), max_columns=20, max_rows=20, cell_height_to_width=1
    )

    assert (preview.width, preview.height) == size
    assert (preview.cell_width, preview.cell_height) == expected_cells
    assert len(preview.rgba) == size[0] * size[1] * 4
