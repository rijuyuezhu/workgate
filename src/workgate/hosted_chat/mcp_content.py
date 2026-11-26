"""LibreChat MCP result formatter (packages/api/src/mcp/parsers.ts).

Raw CallToolResult is never discarded. These fields are an additional model/UI
projection. Image bytes share the pinned native inline-media bound; untrusted remote URLs and
active UI HTML are not projected.
"""

import base64
import json

from .provider_multimodal import inline_media


def _line(value):
    return (
        str(value)
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("\u2028", " ")
        .replace("\u2029", " ")
    )


def _image(item):
    data, mime = item.get("data"), item.get("mimeType", item.get("mime_type"))
    if (
        not isinstance(data, str)
        or not isinstance(mime, str)
        or len(data) > 8 * 1024 * 1024
    ):
        return None
    url = "data:" + mime + ";base64," + data
    if inline_media(url, kind="image_url") is None:
        return None
    return {"type": "image_url", "image_url": {"url": url}}


def _resource_body(resource):
    if resource.get("text"):
        return resource["text"], None, False
    if not resource.get("blob"):
        return None, None, False
    mime = resource.get("mimeType", resource.get("mime_type", ""))
    if str(mime).lower().startswith("image/"):
        return (
            None,
            {"type": "image", "data": resource["blob"], "mimeType": mime},
            False,
        )
    blob = resource["blob"]
    if not isinstance(blob, str) or len(blob) > 8 * 1024 * 1024:
        return (
            "Resource Content: oversized or invalid blob (omitted)",
            None,
            True,
        )
    try:
        binary = base64.b64decode(blob, validate=True)
    except ValueError:
        return "Resource Content: invalid base64 (omitted)", None, True
    try:
        value = binary.decode("utf-8")
        if "\x00" not in value:
            return value, None, False
    except UnicodeError:
        pass
    return (
        f"Resource Content: {len(binary)} bytes of binary data (omitted; not UTF-8 text)",
        None,
        True,
    )


def format_tool_content(
    result: dict, provider: str = "openai"
) -> tuple[str, dict | None]:
    recognized = provider in {
        "openai",
        "openai-responses",
        "azureopenai",
        "openrouter",
        "xai",
        "deepseek",
        "ollama",
        "bedrock",
        "anthropic",
        "google",
    }
    blocks = result.get("content")
    if not isinstance(blocks, list) or not blocks:
        return "(No response)", None
    texts = []
    images = []
    for item in blocks:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            if isinstance(item.get("text"), str):
                texts.append(item["text"])
        elif kind == "image":
            if recognized:
                image = _image(item)
                if image is not None:
                    images.append(image)
                else:
                    texts.append(
                        "[Omitted invalid, remote or oversized MCP image]"
                    )
            else:
                texts.append(json.dumps(item, ensure_ascii=False, indent=2))
        elif kind == "resource":
            resource = item.get("resource") or {}
            if not isinstance(resource, dict):
                continue
            uri = resource.get("uri", "")
            details = []
            if isinstance(uri, str) and uri.startswith("ui://"):
                # Workgate's native MCP Apps boundary owns rendering/authorization.
                # Preserve the full result separately; do not emit executable HTML
                # or unsupported UI marker instructions into model context.
                details.append(
                    "MCP UI resource (not included in model context)"
                )
            else:
                body, image, binary = _resource_body(resource)
                if image and recognized:
                    projected = _image(image)
                    if projected is not None:
                        images.append(projected)
                    else:
                        details.append(
                            "[Omitted invalid, remote or oversized MCP image]"
                        )
                elif isinstance(body, str) and body:
                    details.append(
                        "Resource Text: " + body
                        if recognized and not binary
                        else body
                    )
            if uri:
                details.append("Resource URI: " + _line(uri))
            mime = resource.get("mimeType", resource.get("mime_type"))
            if mime:
                details.append(
                    ("Resource MIME Type: " if recognized else "Type: ")
                    + _line(mime)
                )
            if details:
                texts.append("\n".join(details))
        elif kind == "resource_link":
            details = []
            for key, label in (
                ("name", "Resource Name"),
                ("title", "Resource Title"),
                ("description", "Resource Description"),
                ("uri", "Resource URI"),
                ("mimeType", "Resource MIME Type"),
                ("size", "Resource Size"),
            ):
                if key in item and item[key] is not None:
                    details.append(
                        f"{label}: {_line(item[key])}"
                        + (" bytes" if key == "size" else "")
                    )
            if details:
                texts.append("\n".join(details))
        else:
            texts.append(json.dumps(item, ensure_ascii=False, indent=2))
    artifacts = {"content": images} if images else {}
    text = "\n\n".join(filter(None, texts))
    return text or ("" if artifacts else "(No response)"), artifacts or None


def mcp_result_envelope(raw):
    """Recognize the redacted native Agent Bridge result, not arbitrary tool JSON."""
    if (
        isinstance(raw, dict)
        and isinstance(raw.get("content"), list)
        and all(
            isinstance(part, dict) and isinstance(part.get("type"), str)
            for part in raw["content"]
        )
    ):
        return raw
    return None
