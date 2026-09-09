"""Shared query parsing and payload helpers for terminal image previews."""

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ...utils.image_preview import make_image_preview
from .common import bounded_int as _bounded_int


@dataclass(frozen=True)
class UiImagePreviewRequest:
    """Validated terminal viewport constraints for one image thumbnail."""

    columns: int
    """Maximum terminal columns available to the preview."""
    rows: int
    """Maximum terminal rows available to the preview."""
    cell_aspect: float
    """Terminal cell height-to-width ratio."""


def _bounded_float(
    value: Any,
    *,
    field: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    if value in {None, ""}:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not minimum <= normalized <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return normalized


def image_preview_request(
    params: Mapping[str, Any],
) -> UiImagePreviewRequest | None:
    """Return terminal preview dimensions only when a UI client requested them."""
    if params.get("columns") in {None, ""} and params.get("rows") in {None, ""}:
        return None
    return UiImagePreviewRequest(
        columns=_bounded_int(
            params.get("columns"),
            field="columns",
            default=80,
            minimum=2,
            maximum=200,
        ),
        rows=_bounded_int(
            params.get("rows"),
            field="rows",
            default=24,
            minimum=1,
            maximum=100,
        ),
        cell_aspect=_bounded_float(
            params.get("cell_aspect"),
            field="cell_aspect",
            default=2.0,
            minimum=0.5,
            maximum=5.0,
        ),
    )


def terminal_image_fields(
    data: bytes,
    request: UiImagePreviewRequest | None,
) -> dict[str, Any]:
    """Return optional RGBA thumbnail fields while preserving browser payloads."""
    if request is None:
        return {}
    preview = make_image_preview(
        data,
        request.columns,
        request.rows,
        request.cell_aspect,
    )
    return {
        "rgba": base64.b64encode(preview.rgba).decode("ascii"),
        "width": preview.width,
        "height": preview.height,
        "cell_width": preview.cell_width,
        "cell_height": preview.cell_height,
        "original_width": preview.original_width,
        "original_height": preview.original_height,
    }
