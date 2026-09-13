"""Typed structured outputs for transfer tools."""

from pydantic import BaseModel, Field


class TransferStatOutput(BaseModel):
    """File, directory, or other path metadata for transfer planning."""

    path: str = Field(description="Workspace-relative path that was inspected.")
    type: str = Field(description="Path type: file, dir, or other.")
    size: int | None = Field(
        description="Size in bytes for files and other path types, or null for directories."
    )
    modified: float = Field(
        description="Last modification time as a Unix timestamp."
    )
    sha256: str | None = Field(
        default=None, description="Optional SHA-256 digest for file contents."
    )


class TransferCopyFileOutput(BaseModel):
    """Result of a worker-local raw streaming file copy."""

    source_path: str = Field(
        description="Resolved source path that was copied."
    )
    path: str = Field(
        description="Resolved destination path that was published."
    )
    bytes: int = Field(description="Number of raw bytes copied.")
    sha256: str = Field(description="SHA-256 digest of the copied contents.")
    chunks: int = Field(description="Number of bounded raw chunks copied.")
    chunk_size: int = Field(description="Maximum raw chunk size in bytes.")
    completed: bool = Field(
        description="Whether the destination was atomically published."
    )


class TransferReadChunkOutput(BaseModel):
    """Base64-encoded chunk read from a file."""

    path: str = Field(description="Workspace-relative file path that was read.")
    offset: int = Field(description="Byte offset where this chunk starts.")
    bytes: int = Field(
        description="Number of raw bytes included in this chunk."
    )
    size: int = Field(description="Total source file size in bytes.")
    eof: bool = Field(
        description="Whether this chunk reaches the end of the file."
    )
    sha256: str = Field(description="SHA-256 digest of the source file.")
    data_b64: str = Field(description="Base64-encoded chunk payload.")


class TransferBeginWriteOutput(BaseModel):
    """State for a newly started chunked file write."""

    path: str = Field(description="Workspace-relative destination path.")
    temp_path: str = Field(
        description="Temporary file path used during the write."
    )
    transfer_id: str = Field(
        description="Opaque identifier for this write transfer."
    )
    created: bool = Field(
        description="Whether the destination did not already exist."
    )
    expected_bytes: int | None = Field(
        default=None, description="Expected final byte count, when provided."
    )
    offset: int = Field(
        default=0,
        description="Validated contiguous byte offset already present.",
    )
    resumed: bool = Field(
        default=False,
        description="Whether an existing transaction was resumed.",
    )
    completed: bool = Field(
        default=False,
        description="Whether this transfer id already committed the destination.",
    )
    sha256: str | None = Field(
        default=None,
        description="Committed destination SHA-256 when completion was recovered.",
    )


class TransferWriteChunkOutput(BaseModel):
    """Result of writing one chunk to a temporary transfer file."""

    path: str = Field(description="Workspace-relative destination path.")
    temp_path: str = Field(description="Temporary file path receiving chunks.")
    offset: int = Field(description="Byte offset where the chunk was written.")
    bytes: int = Field(
        description="Number of raw bytes written from this chunk."
    )
    sha256: str = Field(description="SHA-256 digest of bytes written so far.")


class TransferFinishWriteOutput(BaseModel):
    """Result of atomically completing a chunked file write."""

    path: str = Field(description="Workspace-relative destination path.")
    bytes: int = Field(description="Final file size in bytes.")
    sha256: str | None = Field(
        default=None,
        description="SHA-256 digest of the completed file, when available.",
    )
    completed: bool = Field(
        description="Whether the temporary file was moved into place."
    )


class TransferAbortWriteOutput(BaseModel):
    """Result of removing an in-progress temporary transfer file."""

    path: str = Field(description="Workspace-relative destination path.")
    temp_path: str = Field(
        description="Temporary file path targeted for cleanup."
    )
    deleted: bool = Field(description="Whether the temporary file was deleted.")


class TransferAllocTempPathOutput(BaseModel):
    """Allocated workspace-relative temporary transfer path."""

    path: str = Field(
        description="Allocated workspace-relative temporary path."
    )


class TransferDeleteTempPathOutput(BaseModel):
    """Result of removing a transfer scratch file."""

    path: str = Field(description="Transfer scratch path targeted for cleanup.")
    deleted: bool = Field(description="Whether the scratch file was deleted.")


class TransferPackDirOutput(BaseModel):
    """Archive created from a directory for transfer."""

    path: str = Field(
        description="Workspace-relative directory path that was packed."
    )
    archive_path: str = Field(
        description="Workspace-relative archive path that was created."
    )
    bytes: int = Field(description="Archive size in bytes.")
    sha256: str = Field(description="SHA-256 digest of the archive file.")
    compression: str = Field(description="Archive compression format.")


class TransferUnpackArchiveOutput(BaseModel):
    """Archive unpack result."""

    path: str = Field(
        description="Workspace-relative destination directory path."
    )
    archive_path: str = Field(
        description="Workspace-relative archive path that was unpacked."
    )
    entries: int = Field(description="Number of archive entries unpacked.")
    completed: bool = Field(
        description="Whether archive unpacking completed successfully."
    )
    archive_deleted: bool = Field(
        description="Whether the source archive was deleted after unpacking."
    )
    backup_deleted: bool = Field(
        description="Whether the replaced destination backup was removed."
    )
    cleanup_errors: list[str] = Field(
        default_factory=list,
        description="Non-fatal cleanup errors after the destination was committed.",
    )
    resumed: bool = Field(
        default=False,
        description="Whether a prior commit was recovered by transfer identity.",
    )
