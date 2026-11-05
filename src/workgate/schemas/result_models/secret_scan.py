"""Typed structured outputs for secret scanning tools."""

from pydantic import BaseModel, Field


class SecretFinding(BaseModel):
    """One heuristic secret finding."""

    type: str = Field(description="Heuristic pattern name that matched.")
    path: str = Field(
        description="File path containing the finding, relative to the execution session workdir when possible."
    )
    line: int = Field(description="1-based line number containing the finding.")


class SecretScanOutput(BaseModel):
    """Heuristic secret-scan result."""

    findings: list[SecretFinding] = Field(
        description="Returned heuristic secret findings."
    )
    truncated: bool = Field(
        description="Whether the finding list was truncated by the result limit."
    )
    truncated_files: int = Field(
        description="Number of scanned files whose text was truncated before scanning."
    )
    skipped_files: int = Field(
        default=0,
        ge=0,
        description="Number of candidate files skipped because reading failed; findings may be incomplete.",
    )
