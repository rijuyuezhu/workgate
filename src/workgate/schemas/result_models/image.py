"""Structured metadata returned with native MCP image content."""

from pydantic import BaseModel, Field


class ViewImageOutput(BaseModel):
    """Metadata accompanying one native MCP image result."""

    session_id: str = Field(description="Execution session id.")
    path: str = Field(
        description="Resolved display path, session-relative when possible."
    )
    mime_type: str = Field(
        description="Detected image MIME type from file magic."
    )
    bytes: int = Field(description="Original image byte count.")
