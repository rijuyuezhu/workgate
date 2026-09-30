"""Typed structured outputs for file operation tools."""

from pydantic import BaseModel, Field


class EntryInfo(BaseModel):
    """One file-system entry in a directory listing."""

    path: str = Field(description="Workspace-relative entry path.")
    type: str = Field(description="Entry type: file, dir, or other.")
    size: int | None = Field(
        default=None,
        description="File size in bytes, or null for directories and other entries.",
    )
    modified: float = Field(
        description="Last modification time as a Unix timestamp."
    )
    target: str | None = Field(
        default=None,
        description="Raw symlink target for link entries, otherwise omitted.",
        exclude_if=lambda value: value is None,
    )


class ListFilesOutput(BaseModel):
    """Directory listing result."""

    limit_count: int = Field(
        description="Maximum number of entries limited by the request or configuration."
    )
    count: int = Field(description="Number of entries returned in entries.")
    is_truncated: bool = Field(
        description="Whether more entries existed beyond the configured or requested limit."
    )
    entries: list[EntryInfo] = Field(description="Returned directory entries.")


class LineRange(BaseModel):
    """Inclusive 1-based line range shown to the agent."""

    start: int = Field(description="First visible 1-based line number.")
    end: int = Field(description="Final visible 1-based line number.")


class ReadLine(BaseModel):
    """One decoded line with its original file line number."""

    line: int = Field(description="Original 1-based line number in the file.")
    text: str = Field(description="Line text without its trailing newline.")


class ReadFileOutput(BaseModel):
    """UTF-8 text file content plus edit-grounding metadata."""

    path: str = Field(description="Workspace-relative file path that was read.")
    bytes: int = Field(description="Total file size in bytes.")
    bytes_read: int | None = Field(
        default=None,
        description="Number of bytes read into the text response, when applicable.",
    )
    truncated_bytes: int | None = Field(
        default=None,
        description="Number of file bytes omitted due to the read limit, when applicable.",
    )
    total_lines: int | None = Field(
        default=None,
        description="Total decoded text line count before optional line-range selection.",
    )
    start_line: int | None = Field(
        default=None,
        description="First original 1-based line number returned across selected ranges, or null when no lines were returned.",
    )
    end_line: int | None = Field(
        default=None,
        description="Final original 1-based line number returned across selected ranges, or null when no lines were returned.",
    )
    line_count: int = Field(
        default=0,
        description="Number of decoded text lines returned in lines and grounded numbered_content across all selected ranges.",
    )
    lines: list[ReadLine] = Field(
        default_factory=list,
        description="Returned lines with original 1-based line numbers for precise follow-up edits.",
    )
    numbered_content: str = Field(
        default="",
        description="Grounded model-facing text: optional [path#snapshot_id] header plus 'line:text' rows for all selected ranges.",
    )
    session_id: str | None = Field(
        default=None,
        description="Explicit agent/workspace session that recorded this read, or null when no grounding snapshot was recorded.",
    )
    snapshot_id: str | None = Field(
        default=None,
        description="Opaque handle for this displayed file snapshot, used by line-based edit tools to reject stale edits.",
    )
    file_sha256: str | None = Field(
        default=None,
        description="SHA-256 digest of the complete file at the time it was read.",
    )
    seen_ranges: list[LineRange] = Field(
        default_factory=list,
        description="Inclusive original line ranges that were actually shown and are eligible for grounded line edits.",
    )
    truncated: bool = Field(
        default=False,
        description="Whether text content was truncated to fit the read limit.",
    )
    content: str = Field(
        description="Decoded UTF-8 text content. Prefer numbered_content/hashline output for grounded edits."
    )


class ReadFileMetadata(BaseModel):
    """File read metadata without duplicate file text."""

    path: str = Field(description="Workspace-relative file path that was read.")
    bytes: int = Field(description="Total file size in bytes.")
    bytes_read: int | None = Field(
        default=None,
        description="Number of bytes read into the text response, when applicable.",
    )
    truncated_bytes: int | None = Field(
        default=None,
        description="Number of file bytes omitted due to the read limit, when applicable.",
    )
    total_lines: int | None = Field(
        default=None,
        description="Total decoded text line count before optional line-range selection.",
    )
    start_line: int | None = Field(
        default=None,
        description="First original 1-based line number shown across selected ranges, or null when no lines were shown.",
    )
    end_line: int | None = Field(
        default=None,
        description="Final original 1-based line number shown across selected ranges, or null when no lines were shown.",
    )
    line_count: int = Field(
        default=0,
        description="Number of decoded text lines shown across all selected ranges.",
    )
    session_id: str | None = Field(
        default=None,
        description="Explicit agent/workspace session that recorded this read, or null when no grounding snapshot was recorded.",
    )
    snapshot_id: str | None = Field(
        default=None,
        description="Opaque handle for this displayed file snapshot, used by edit tools to reject stale edits.",
    )
    file_sha256: str | None = Field(
        default=None,
        description="SHA-256 digest of the complete file at the time it was read.",
    )
    seen_ranges: list[LineRange] = Field(
        default_factory=list,
        description="Inclusive original line ranges that were actually shown and are eligible for grounded edits.",
    )
    truncated: bool = Field(
        default=False,
        description="Whether text content was truncated to fit the read limit.",
    )

    @classmethod
    def from_read_result(cls, result: ReadFileOutput) -> ReadFileMetadata:
        """Build compact model-facing metadata from an internal file read result."""
        return cls(
            path=result.path,
            bytes=result.bytes,
            bytes_read=result.bytes_read,
            truncated_bytes=result.truncated_bytes,
            total_lines=result.total_lines,
            start_line=result.start_line,
            end_line=result.end_line,
            line_count=result.line_count,
            session_id=result.session_id,
            snapshot_id=result.snapshot_id,
            file_sha256=result.file_sha256,
            seen_ranges=result.seen_ranges,
            truncated=result.truncated,
        )


class WriteFileOutput(BaseModel):
    """File write result."""

    path: str = Field(
        description="Workspace-relative file path that was written."
    )
    bytes: int = Field(description="Number of UTF-8 bytes written.")
    created: bool = Field(
        description="Whether the file did not exist before this write."
    )


class EditContextOutput(BaseModel):
    """Compact fresh grounding returned after an edit."""

    path: str = Field(
        description="Workspace-relative file path for this context."
    )
    snapshot_id: str | None = Field(
        default=None,
        description="Fresh snapshot handle embedded in numbered_content for follow-up edits.",
    )
    start_line: int | None = Field(
        default=None,
        description="First original 1-based line shown in this context, or null when empty.",
    )
    end_line: int | None = Field(
        default=None,
        description="Final original 1-based line shown in this context, or null when empty.",
    )
    numbered_content: str = Field(
        description="Canonical [path#snapshot_id] plus line:text grounding for the next edit."
    )
    truncated: bool = Field(
        default=False,
        description="Whether the underlying bounded read truncated file text.",
    )

    @classmethod
    def from_read_result(cls, result: ReadFileOutput) -> EditContextOutput:
        """Build one compact post-edit grounding context."""
        return cls(
            path=result.path,
            snapshot_id=result.snapshot_id,
            start_line=result.start_line,
            end_line=result.end_line,
            numbered_content=result.numbered_content,
            truncated=result.truncated,
        )


class EditLinesOutput(BaseModel):
    """Grounded whole-line edit result."""

    path: str = Field(
        description="Workspace-relative file path that was edited."
    )
    start_line: int = Field(
        description="Original 1-based first line replaced by this edit."
    )
    end_line: int = Field(
        description="Original 1-based final line replaced by this edit."
    )
    replacement_line_count: int = Field(
        description="Number of replacement lines inserted for the selected range."
    )
    diff: str = Field(description="Unified diff for the applied line edit.")
    context: EditContextOutput = Field(
        description="Compact fresh hashline context around the changed line range."
    )


class HashlineEditHunkOutput(BaseModel):
    """One applied hashline edit hunk."""

    path: str = Field(
        description="Workspace-relative file path edited by this hunk."
    )
    start_line: int = Field(
        description="Original 1-based first line touched by this hunk."
    )
    end_line: int = Field(
        description="Original 1-based final line touched by this hunk."
    )
    replacement_line_count: int = Field(
        description="Number of replacement lines inserted for this hunk."
    )
    context: EditContextOutput = Field(
        description="Compact fresh hashline context around this hunk."
    )


class HashlineEditOutput(BaseModel):
    """Grounded hashline edit result for one or more applied hunks."""

    path: str = Field(
        description="Workspace-relative path of the first edited hunk."
    )
    start_line: int = Field(
        description="Original first edited line, aggregated for one-file edits."
    )
    end_line: int = Field(
        description="Original final edited line, aggregated for one-file edits."
    )
    replacement_line_count: int = Field(
        description="Total number of replacement lines inserted across hunks."
    )
    diff: str = Field(
        description="Combined unified diff for the applied edits."
    )
    hunk_count: int = Field(description="Number of hashline hunks applied.")
    hunks: list[HashlineEditHunkOutput] = Field(
        description="Per-hunk edit summaries in original input order."
    )


class DeleteFileOrDirOutput(BaseModel):
    """File or directory deletion result."""

    path: str = Field(description="Workspace-relative path that was deleted.")
    deleted: str = Field(
        description="Deleted item type, usually file or directory."
    )
