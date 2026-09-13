"""Typed structured outputs for control-owned public file links."""

from pydantic import BaseModel, Field


class FileLinkSummary(BaseModel):
    """Non-secret management summary of one public file link."""

    link_id: str = Field(description="Opaque non-secret management identifier.")
    token_fingerprint: str = Field(
        description="Short non-secret fingerprint of the unrecoverable bearer token."
    )
    path: str | None = None
    filename: str | None = None
    inline: bool = False
    media_type: str | None = None
    bytes: int | None = None
    created_at: float | None = None
    expires_at: float | None = None
    ttl_remaining_s: int = 0
    downloads: int = 0
    max_downloads: int = 0


class CreateFileLinkOutput(FileLinkSummary):
    """Created file link; bearer credentials are returned only at creation."""

    token: str = Field(description="Sensitive bearer token returned only once.")
    url: str = Field(
        description="Browser-accessible URL containing the bearer token."
    )


class ListFileLinksOutput(BaseModel):
    """Non-secret public-link management listing."""

    links: list[FileLinkSummary]


class RevokeFileLinkOutput(BaseModel):
    """Result of revoking a public link by management id."""

    revoked: bool
    link_id: str
