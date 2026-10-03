"""Typed input annotations for Todo compatibility tools."""

from typing import Annotated, Any

from pydantic import Field

TodosArg = Annotated[
    list[dict[str, Any]],
    Field(
        description="Replacement Todo projection. Each item may include id, content, status, and priority."
    ),
]
