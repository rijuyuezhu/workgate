"""Typed structured outputs for tokenized download-link tools."""

from pydantic import BaseModel, Field


class FileLinkSummary(BaseModel):
    """Public summary of a tokenized download link."""

    token: str = Field(
        description="Sensitive token identifying the download link."
    )
    url: str = Field(
        description="Browser-accessible download URL containing the token."
    )
    path: str | None = Field(description="Workspace-relative source file path.")
    filename: str | None = Field(description="Browser response filename.")
    inline: bool = Field(
        description="Whether the browser response uses inline disposition."
    )
    media_type: str | None = Field(
        description="Stored MIME type for the browser response."
    )
    bytes: int | None = Field(
        description="Creation-time snapshot size in bytes."
    )
    created_at: float | None = Field(
        description="Unix timestamp when the link was created."
    )
    expires_at: float | None = Field(
        description="Unix timestamp when the link expires."
    )
    ttl_remaining_s: int = Field(
        description="Approximate remaining lifetime in seconds."
    )
    downloads: int = Field(
        description="Number of completed downloads recorded so far."
    )
    max_downloads: int = Field(
        description="Maximum allowed downloads, or 0 for unlimited."
    )


class CreateFileLinkOutput(FileLinkSummary):
    """Created tokenized download-link summary."""


class ListFileLinksOutput(BaseModel):
    """Tokenized download-link listing."""

    links: list[FileLinkSummary] = Field(
        description="Download-link summaries sorted newest first."
    )


class RevokeFileLinkOutput(BaseModel):
    """Download-link revocation result."""

    revoked: bool = Field(
        description="Whether a stored link was found and removed."
    )
    token: str = Field(description="Token that was requested for revocation.")
