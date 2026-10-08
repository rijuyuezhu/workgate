"""Typed outputs for control-owned audit queries."""

from typing import Any

from pydantic import BaseModel, Field


class AuditTailOutput(BaseModel):
    """Bounded logical audit query result from canonical control history."""

    task_id: str | None = None
    """Semantic task scope, when requested."""
    session_id: str | None = None
    """Concrete execution-session scope, when requested."""
    entries: list[dict[str, Any]] = Field(default_factory=list)
    """Bounded logical audit entries with references or resolved retained values."""
    count: int
    """Number of entries returned in this response."""
    total_matched: int
    """Number of logical entries matching the filters before the limit."""
    failed_matched: int
    """Number of matching logical entries with failed status."""
    entry_id: str | None = None
    """Requested stable logical entry id for detail mode."""
    full_payloads: bool = False
    """Whether retained audit payload references were requested for resolution."""
