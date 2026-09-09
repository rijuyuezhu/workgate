"""Dependency-light image type detection shared by UI and executor code."""


def detect_image_type(header: bytes) -> tuple[str, str]:
    """Detect one supported image type from bounded file magic."""
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg", "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "gif", "image/gif"
    if (
        len(header) >= 12
        and header.startswith(b"RIFF")
        and header[8:12] == b"WEBP"
    ):
        return "webp", "image/webp"
    raise ValueError(
        "Unsupported image format; expected PNG, JPEG, GIF, or WebP"
    )
