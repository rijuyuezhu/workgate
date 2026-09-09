"""Connector-compatible error projections for workspace tools."""

from typing import Any

from ...errors import public_error_type
from ..schemas.result_models.workspace_connector import (
    FetchOutput,
    SearchOutput,
)


def search_error_output(
    exc: Exception, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> SearchOutput:
    """Return connector-compatible search output for MCP tool errors."""
    return SearchOutput(results=[])


def _fetch_id_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    if "id" in kwargs:
        return str(kwargs["id"])
    if args:
        return str(args[0])
    return ""


def fetch_error_output(
    exc: Exception, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> FetchOutput:
    """Return connector-compatible fetch output for MCP tool errors."""
    id = _fetch_id_from_call(args, kwargs)
    error_type = public_error_type(exc)
    return FetchOutput(
        id=id,
        title=id,
        text=f"Unable to fetch file: {error_type}: {exc}",
        url=f"file:///workspace/{id}",
        metadata={
            "source": "workspace",
            "error": error_type,
        },
    )
