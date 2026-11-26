"""Canonical Chat Completions content parts -> native Anthropic/Gemini parts.

No client-originated media URLs are fetched. The generation boundary must
provide owner-authorized attachment bytes or redacted native MCP media.
"""

import base64

_IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})


def inline_media(data_url: object, *, kind: str) -> tuple[str, str] | None:
    """Only canonical bounded inline bytes, never a remote URL or file ID."""
    if (
        not isinstance(data_url, str)
        or len(data_url) > 8 * 1024 * 1024
        or not data_url.startswith("data:")
        or ";base64," not in data_url
    ):
        return None
    mime, data = data_url[5:].split(";base64,", 1)
    if (
        (kind == "image_url" and mime not in _IMAGE_MIMES)
        or (kind == "file" and mime != "application/pdf")
        or kind not in {"image_url", "file"}
    ):
        return None
    try:
        if (
            not data
            or base64.b64encode(base64.b64decode(data, validate=True)).decode(
                "ascii"
            )
            != data
        ):
            return None
    except ValueError:
        return None
    return mime, data


def native_content_parts(content, provider: str) -> list[dict]:
    if provider not in {"anthropic", "google"}:
        raise ValueError("Unsupported native media provider")
    if isinstance(content, str):
        return (
            [
                {"type": "text", "text": content}
                if provider == "anthropic"
                else {"text": content}
            ]
            if content
            else []
        )
    if not isinstance(content, list):
        return []
    result = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            result.append(
                {"type": "text", "text": part["text"]}
                if provider == "anthropic"
                else {"text": part["text"]}
            )
        elif kind in {"image_url", "file"}:
            fields = (
                part.get("image_url")
                if kind == "image_url"
                else part.get("file")
            )
            if not isinstance(fields, dict):
                continue
            data_url = fields.get("url" if kind == "image_url" else "file_data")
            media = inline_media(data_url, kind=kind)
            if media is None:
                continue
            mime, data = media
            if provider == "anthropic":
                result.append(
                    {
                        "type": "image"
                        if mime.startswith("image/")
                        else "document",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": data,
                        },
                    }
                )
            else:
                result.append({"inlineData": {"mimeType": mime, "data": data}})
    return result
