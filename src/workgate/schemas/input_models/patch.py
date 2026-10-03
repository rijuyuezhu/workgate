"""Typed input annotations for patch application tools."""

from typing import Annotated

from pydantic import Field

PatchTextArg = Annotated[
    str,
    Field(
        description="A standard unified diff or an apply_patch envelope beginning with '*** Begin Patch'. Patch paths must stay within cwd."
    ),
]

PatchCwdArg = Annotated[
    str,
    Field(
        description="Directory against which patch paths are resolved. Relative values resolve from the session workdir."
    ),
]
