"""Keep native GUI screenshots small without weakening capture or MIME contracts."""

import base64
import random
from io import BytesIO

import pytest
from mcp.types import ImageContent
from PIL import Image

from tests.executor.test_gui_service import service
from workgate.executor.gui import base as gui_base
from workgate.utils.image_types import detect_image_type


def _image_bytes(
    mode: str = "RGB", *, width: int = 400, height: int = 400
) -> bytes:
    random_pixels = random.Random(42).randbytes(width * height * 3)
    frame = Image.frombytes("RGB", (width, height), random_pixels)
    if mode == "RGBA":
        frame = frame.convert("RGBA")
        frame.putpixel((0, 0), (255, 0, 0, 0))
    with BytesIO() as output:
        frame.save(output, format="PNG")
        return output.getvalue()


def test_large_opaque_png_compresses_and_keeps_dimensions():
    original = _image_bytes()
    optimized = gui_base._compress_gui_screenshot(original)
    assert len(optimized) < len(original)
    image_type, mime_type = detect_image_type(optimized[:16])
    assert image_type in {"webp", "jpeg"}
    assert mime_type in {"image/webp", "image/jpeg"}
    with Image.open(BytesIO(optimized)) as image:
        assert image.size == (400, 400)


def test_transparent_and_small_captures_are_left_untouched():
    transparent = _image_bytes("RGBA")
    assert len(transparent) >= gui_base.GUI_SCREENSHOT_COMPRESS_MIN_BYTES
    assert gui_base._compress_gui_screenshot(transparent) == transparent
    with BytesIO() as output:
        Image.new("RGB", (40, 30), "white").save(output, format="PNG")
        original = output.getvalue()
    assert gui_base._compress_gui_screenshot(original) == original


def test_oversized_pixel_dimensions_and_non_png_unchanged(monkeypatch):
    original = _image_bytes()
    monkeypatch.setattr(gui_base, "GUI_SCREENSHOT_COMPRESS_MAX_PIXELS", 1)
    assert gui_base._compress_gui_screenshot(original) == original
    with BytesIO() as output:
        Image.new("RGB", (400, 400), "white").save(output, format="JPEG")
        jpeg = output.getvalue()
    assert gui_base._compress_gui_screenshot(jpeg) == jpeg


def test_jpeg_fallback_when_webp_unavailable(monkeypatch):
    monkeypatch.setattr(gui_base.features, "check", lambda _feature: False)
    original = _image_bytes()
    compressed = gui_base._compress_gui_screenshot(original)
    assert detect_image_type(compressed[:16]) == ("jpeg", "image/jpeg")
    assert len(compressed) < len(original)


@pytest.mark.asyncio
async def test_gui_state_compresses_native_image_and_advertises_correct_mime(
    tmp_path,
):
    gui, backend, _store, session_id, _second = service(tmp_path)
    backend.bounds = {"x": 100, "y": 200, "width": 400, "height": 400}
    original = _image_bytes()
    native_snapshot = backend.snapshot

    async def capture(*args, **kwargs):
        snapshot = await native_snapshot(*args, **kwargs)
        screenshot_path = kwargs.get("screenshot_path")
        if screenshot_path is not None:
            screenshot_path.write_bytes(original)
        return snapshot

    backend.snapshot = capture
    result = await gui.snapshot(session_id, "window-1")
    frame = next(
        item for item in result.content if isinstance(item, ImageContent)
    )
    image_data = base64.b64decode(frame.data)
    assert len(image_data) < len(original)
    assert frame.mime_type == detect_image_type(image_data[:16])[1]
    assert result.structured_content["screenshot"] == {
        "mime_type": frame.mime_type,
        "bytes": len(image_data),
    }
