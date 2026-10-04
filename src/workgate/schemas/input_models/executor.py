"""Typed input annotations for executor fleet administration."""

from typing import Annotated, Literal

from pydantic import Field

ExecutorActionArg = Annotated[
    Literal[
        "list",
        "inspect",
        "rename",
        "reset",
        "drain",
        "resume",
        "revoke",
        "bootstrap",
    ],
    Field(description="Executor fleet discovery or administration action."),
]
ExecutorIdArg = Annotated[
    str | None,
    Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Executor id required by executor-specific actions.",
    ),
]
ExecutorNameArg = Annotated[
    str | None,
    Field(
        default=None,
        min_length=1,
        max_length=80,
        description="New executor display name for action='rename'.",
    ),
]
