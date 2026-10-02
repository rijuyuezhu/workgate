"""Typed input annotations for Todo compatibility tools."""

from typing import Annotated, Any

from pydantic import Field

ExpectedTodoRevisionArg = Annotated[
    int | None,
    Field(
        default=None,
        ge=0,
        strict=True,
        description="Optional task revision that must still match before replacement.",
    ),
]


TodosArg = Annotated[
    list[dict[str, Any]],
    Field(
        description="Replacement Todo projection. Each item may include id, content, status, and priority."
    ),
]
