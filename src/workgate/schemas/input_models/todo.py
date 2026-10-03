"""Typed input annotations for Todo compatibility tools."""

from typing import Annotated, Any

from pydantic import Field

TodosArg = Annotated[
    list[dict[str, Any]],
    Field(
        description=(
            "Replacement Todo projection. Items support id, content, status, and "
            "priority; status is pending, in_progress, completed, skipped, or blocked."
        )
    ),
]
