"""Bounded RGBA thumbnails for terminal-native image previews."""

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps

MAX_PREVIEW_SOURCE_PIXELS = 25_000_000
SUPER_SAMPLE_PIXELS_PER_CELL = 2


@dataclass(frozen=True)
class ImagePreview:
    """One RGBA thumbnail sized for OpenTUI's supersampled pixel buffer."""

    rgba: bytes
    """Raw row-major RGBA8 thumbnail bytes."""
    width: int
    """Thumbnail pixel width."""
    height: int
    """Thumbnail pixel height."""
    cell_width: int
    """Terminal cell width required by the supersampled renderer."""
    cell_height: int
    """Terminal cell height required by the supersampled renderer."""
    original_width: int
    """Source image width before orientation and resizing."""
    original_height: int
    """Source image height before orientation and resizing."""


def make_image_preview(
    data: bytes,
    max_columns: int,
    max_rows: int,
    cell_height_to_width: float = 2.0,
) -> ImagePreview:
    """Decode a bounded first-frame RGBA thumbnail from validated image bytes."""
    columns = max(2, min(int(max_columns), 200))
    rows = max(1, min(int(max_rows), 100))
    cell_aspect = max(0.5, min(float(cell_height_to_width), 5.0))
    max_pixel_width = columns * SUPER_SAMPLE_PIXELS_PER_CELL
    max_pixel_height = rows * SUPER_SAMPLE_PIXELS_PER_CELL

    try:
        with Image.open(BytesIO(data)) as opened:
            opened.seek(0)
            source_width, source_height = opened.size
            if source_width <= 0 or source_height <= 0:
                raise ValueError("Image has invalid dimensions")
            source_pixels = source_width * source_height
            if source_pixels > MAX_PREVIEW_SOURCE_PIXELS:
                raise ValueError(
                    "Refusing to decode image preview with "
                    f"{source_pixels} pixels; max is {MAX_PREVIEW_SOURCE_PIXELS}"
                )
            oriented = ImageOps.exif_transpose(opened)
            original_width, original_height = oriented.size
            frame = oriented.convert("RGBA")
            compensated_height = original_height / cell_aspect
            scale = min(
                1.0,
                max_pixel_width / original_width,
                max_pixel_height / compensated_height,
            )
            target_width = max(
                1, min(max_pixel_width, int(original_width * scale))
            )
            target_height = max(
                1, min(max_pixel_height, int(compensated_height * scale))
            )
            if frame.size != (target_width, target_height):
                frame = frame.resize(
                    (target_width, target_height),
                    Image.Resampling.LANCZOS,
                    reducing_gap=3.0,
                )
            width, height = frame.size
            rgba = frame.tobytes()

    except (Image.DecompressionBombError, OSError) as exc:
        raise ValueError(f"Unable to decode image preview: {exc}") from exc

    return ImagePreview(
        rgba=rgba,
        width=width,
        height=height,
        cell_width=max(2, (width + 1) // 2),
        cell_height=(height + 1) // 2,
        original_width=original_width,
        original_height=original_height,
    )
