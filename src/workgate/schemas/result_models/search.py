"""Typed structured outputs for search and tree-view tools."""

from typing import Literal

from pydantic import BaseModel, Field

from .files import LineRange


class TreeViewOutput(BaseModel):
    """Compact directory tree result."""

    root: str = Field(description="Resolved root path used for the tree view.")
    exists: bool = Field(description="Whether the requested tree root exists.")
    is_directory: bool = Field(
        description="Whether the requested root is a directory."
    )
    entries: list[str] = Field(
        description="Indented tree entries relative to root."
    )
    count: int = Field(description="Number of entries returned.")
    truncated: bool = Field(
        description="Whether additional entries were omitted due to limits."
    )
    message: str | None = Field(
        default=None,
        description="Optional diagnostic message for missing or non-directory roots.",
    )
    nearest_existing_parent: str | None = Field(
        default=None,
        description="Nearest existing parent for a missing root, when available.",
    )
    nearest_parent_entries: list[str] | None = Field(
        default=None,
        description="Entries in the nearest existing parent for a missing root.",
    )
    nearest_parent_entries_truncated: bool | None = Field(
        default=None,
        description="Whether nearest_parent_entries was truncated.",
    )


class GlobSearchOutput(BaseModel):
    """Glob file search result."""

    paths: list[str] = Field(
        description="Matching paths, relative to the execution session workdir when possible."
    )


class GrepMatch(BaseModel):
    """One ripgrep match."""

    path: str | None = Field(
        description="Path containing the match, when reported by ripgrep."
    )
    line: int | None = Field(
        description="1-based line number containing the match."
    )
    column: int | None = Field(
        description="1-based column number for the first match on the line."
    )
    text: str = Field(
        description="Matching line text without the trailing newline."
    )
    numbered_line: str | None = Field(
        default=None,
        description="Grounded match text with optional hashline header plus 'line:text' row.",
    )
    session_id: str | None = Field(
        default=None,
        description="Agent grounding session that recorded this match line.",
    )
    snapshot_id: str | None = Field(
        default=None,
        description="Snapshot handle for the displayed match line, usable with hashline_edit and edit_lines.",
    )
    file_sha256: str | None = Field(
        default=None,
        description="SHA-256 digest of the complete matched file when displayed.",
    )
    seen_range: LineRange | None = Field(
        default=None,
        description="Inclusive original line range shown for this match.",
    )


class GrepDisplayLine(BaseModel):
    """One displayed search output line, either an actual match or context."""

    path: str | None = Field(description="Path containing the displayed line.")
    line: int = Field(description="1-based displayed line number.")
    kind: Literal["match", "context"] = Field(
        description="Whether this displayed line is an actual match or surrounding context."
    )
    text: str = Field(
        description="Displayed line text without the trailing newline."
    )
    numbered_line: str = Field(
        description="Copyable 'line:text' row for this displayed line; use with the enclosing [path#snapshot_id] header from numbered_content."
    )
    session_id: str | None = Field(
        default=None,
        description="Agent grounding session that recorded this displayed line.",
    )
    snapshot_id: str | None = Field(
        default=None,
        description="Snapshot handle for the displayed context window, usable with hashline_edit and edit_lines.",
    )
    file_sha256: str | None = Field(
        default=None,
        description="SHA-256 digest of the complete matched file when displayed.",
    )
    seen_range: LineRange | None = Field(
        default=None,
        description="Inclusive original line range shown in this displayed context window.",
    )


class GrepSearchOutput(BaseModel):
    """Ripgrep content search result."""

    ok: bool = Field(
        description="Whether ripgrep completed successfully or with no matches."
    )
    matches: list[GrepMatch] = Field(description="Returned ripgrep matches.")
    displayed_lines: list[GrepDisplayLine] = Field(
        default_factory=list,
        description="Displayed hashline rows with kind='match' for actual matches and kind='context' for surrounding context.",
    )
    count: int = Field(description="Number of matches returned.")
    displayed_count: int = Field(
        default=0,
        description="Number of displayed match and context lines returned.",
    )
    context_radius: int = Field(
        default=0,
        description="Number of surrounding context lines requested around each returned match.",
    )
    skipped: int = Field(
        default=0,
        description="Number of earlier matches skipped before the returned page.",
    )
    truncated: bool = Field(
        description="Whether results were truncated by match or output limits."
    )
    stderr: str = Field(
        description="Captured ripgrep stderr, after output limiting."
    )
    numbered_content: str = Field(
        default="",
        description="Grouped grounded hashline search snippets with [path#snapshot_id] headers and copyable line:text rows; use displayed_lines.kind to distinguish actual matches from context.",
    )
