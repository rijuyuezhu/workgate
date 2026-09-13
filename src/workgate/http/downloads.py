"""Public HTTP routes for tokenized file downloads."""

import hashlib
from collections.abc import Iterator
from functools import partial
from typing import Any
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ..audit import audit
from ..config.control import ControlSettingsView
from ..control.download_store import ClaimedDownload, release_claim
from ..control.downloads import DOWNLOAD_PREFIX, claim_download


def download_error_response(payload: dict[str, Any]) -> JSONResponse:
    """Convert an operation-level download error payload into JSONResponse."""
    status_code = int(payload.get("status_code", 500))
    return JSONResponse(
        {
            "ok": False,
            "error": str(payload.get("error", "download_error")),
            "message": str(payload.get("message", "Download failed")),
        },
        status_code=status_code,
    )


def _token_fingerprint(token: str) -> str:
    """Return a short non-secret fingerprint for a download token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _content_disposition(filename: str, *, inline: bool) -> str:
    """Return a safe RFC 6266 content-disposition value."""
    disposition = "inline" if inline else "attachment"
    fallback = (
        filename.encode("ascii", errors="ignore")
        .decode("ascii")
        .replace('"', "")
        .replace("\\", "")
        .strip()
        or "download"
    )
    encoded = quote(filename, safe="")
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def _stream_claim(claim: ClaimedDownload) -> Iterator[bytes]:
    """Yield a claimed snapshot and release it after response completion."""
    try:
        while chunk := claim.handle.read(64 * 1024):
            yield chunk
    finally:
        release_claim(claim)


async def download_endpoint(
    request: Request, *, settings: ControlSettingsView | None = None
) -> Response:
    """Serve a tokenized immutable snapshot without requiring bearer auth."""
    token = request.path_params.get("token", "")
    is_get = request.method.upper() == "GET"
    claimed = claim_download(token, consume=is_get, settings=settings)
    if isinstance(claimed, dict):
        return download_error_response(claimed)

    link = claimed.link
    filename = str(link.get("filename") or claimed.path.name)
    media_type = str(link.get("media_type") or "application/octet-stream")
    inline = bool(link.get("inline", False))
    headers = {
        "Cache-Control": "private, no-store",
        "Content-Disposition": _content_disposition(filename, inline=inline),
        "Content-Length": str(int(link.get("bytes", 0))),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    if inline:
        headers["Content-Security-Policy"] = "sandbox"
    audit(
        "download_link_served",
        path=link.get("display_path"),
        token_sha256=_token_fingerprint(token),
        method=request.method,
        inline=inline,
        media_type=media_type,
    )
    if not is_get:
        release_claim(claimed)
        return Response(status_code=200, headers=headers, media_type=media_type)
    return StreamingResponse(
        _stream_claim(claimed),
        media_type=media_type,
        headers=headers,
    )


def download_routes(
    settings: ControlSettingsView | None = None,
) -> list[Route]:
    """Return public Starlette routes for generated download links."""
    endpoint = (
        download_endpoint
        if settings is None
        else partial(download_endpoint, settings=settings)
    )
    return [
        Route(
            f"{DOWNLOAD_PREFIX}/{{token}}",
            endpoint,
            methods=["GET", "HEAD"],
        )
    ]
