"""Typed input annotations for file operation tools."""

from typing import Annotated

from pydantic import Field

FilePathArg = Annotated[
    str,
    Field(
        description="Path for the file or directory operation. Relative values resolve from the execution session workdir; absolute paths are allowed."
    ),
]
ListPathArg = Annotated[
    str,
    Field(
        description="Directory path to list. Relative paths resolve from the execution session workdir."
    ),
]
RecursiveArg = Annotated[
    bool,
    Field(
        description="Whether to recurse into descendant directories. Required for deleting non-empty directories."
    ),
]
MaxEntriesArg = Annotated[
    int,
    Field(
        description="Maximum number of entries to return before reporting truncation. Bounded by the server configuration."
    ),
]
ToolSessionIdArg = Annotated[
    str | None,
    Field(
        description="Optional execution session id returned by session_start. Internal helpers may omit it when no grounding snapshot is needed."
    ),
]
EditStartLineArg = Annotated[
    int,
    Field(
        description="1-based first original line to replace. The range is inclusive."
    ),
]
EditEndLineArg = Annotated[
    int,
    Field(
        description="1-based final original line to replace. The range is inclusive."
    ),
]
LineReplacementArg = Annotated[
    str,
    Field(
        description="Replacement text for the selected whole-line range. Use an empty string to delete the range."
    ),
]
SnapshotIdArg = Annotated[
    str | None,
    Field(
        description="Optional snapshot_id returned by read or search; copy it exactly, do not invent it. When provided, the edit is rejected if the file changed or the line range was not shown."
    ),
]


HashlineEditInputArg = Annotated[
    str,
    Field(
        description="Hashline edit text starting with [path#snapshot_id]. Copy the header/tag and displayed line:text rows from read/search; do not invent snapshot ids. Add + final-content rows, or use SWAP/INSERT directives. Separate multiple non-overlapping hunks with blank lines or repeated headers."
    ),
]

FileContentArg = Annotated[
    str,
    Field(
        description="Complete UTF-8 text content to write to the target file."
    ),
]
OverwriteArg = Annotated[
    bool,
    Field(
        description="Whether to replace an existing file. Set false to fail when the target already exists."
    ),
]
