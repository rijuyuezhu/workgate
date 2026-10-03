"""Typed input annotations for secret scanning tools."""

from typing import Annotated

from pydantic import Field

SecretScanCwdArg = Annotated[
    str,
    Field(
        description="Directory to scan. Relative paths resolve from the execution session workdir."
    ),
]
SecretScanGlobArg = Annotated[
    str | None,
    Field(
        description="Optional glob pattern to narrow which files are scanned."
    ),
]
SecretScanMaxResultsArg = Annotated[
    int,
    Field(
        description="Maximum number of findings to return before reporting truncation. Bounded by server configuration."
    ),
]
