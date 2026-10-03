"""Typed input annotations for session-bound audit queries."""

from typing import Annotated, Literal

from pydantic import Field

AuditLimitArg = Annotated[
    int,
    Field(
        default=100,
        ge=1,
        le=2_000,
        description="Maximum logical audit entries to return.",
    ),
]
AuditEventArg = Annotated[
    str | None,
    Field(
        default=None,
        max_length=256,
        description="Optional case-insensitive event-name filter.",
    ),
]
AuditOperationArg = Annotated[
    str | None,
    Field(
        default=None,
        max_length=128,
        description="Optional normalized operation-category filter.",
    ),
]
AuditSearchArg = Annotated[
    str | None,
    Field(
        default=None,
        max_length=1_000,
        description="Optional metadata-only case-insensitive search text.",
    ),
]
AuditTimestampArg = Annotated[
    float | None,
    Field(default=None, ge=0, description="Optional Unix timestamp boundary."),
]
AuditSortArg = Annotated[
    Literal["asc", "desc"],
    Field(default="desc", description="Timestamp sort order."),
]
AuditEntryIdArg = Annotated[
    str | None,
    Field(
        default=None,
        max_length=256,
        description="Stable logical audit entry id to retrieve.",
    ),
]
AuditIncludeFullPayloadsArg = Annotated[
    bool,
    Field(
        default=False,
        description="Resolve retained sanitized payload objects. Requires audit:full and entry_id.",
    ),
]
