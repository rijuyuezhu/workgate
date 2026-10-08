"""File operation tool registry."""

from ...schemas.input_models.files import (
    EditEndLineArg,
    EditStartLineArg,
    FileContentArg,
    FilePathArg,
    HashlineEditInputArg,
    LineReplacementArg,
    ListPathArg,
    MaxEntriesArg,
    OverwriteArg,
    RecursiveArg,
    SnapshotIdArg,
)
from ...schemas.input_models.session import SessionIdArg
from ...schemas.result_models.files import (
    DeleteFileOrDirOutput,
    EditLinesOutput,
    HashlineEditOutput,
    ListFilesOutput,
    WriteFileOutput,
)
from ..contracts import McpToolContext
from ..declarative import DeclarativeToolRegistry


class FileToolRegistry(DeclarativeToolRegistry):
    """Register file operation tool declarations."""

    name = "file"


file_tool = FileToolRegistry.get_tool_decorator()


def _list_files_description(context: McpToolContext) -> str:
    del context
    return """List files and directories under an execution session workdir. The result reports whether entries were truncated by the requested limit or server cap."""


def _write_file_description(context: McpToolContext) -> str:
    del context
    return """Write a complete UTF-8 file inside an execution session. Use it for new files or intentional whole-file replacement; prefer hashline_edit for ordinary edits to existing files."""


def _edit_lines_description(context: McpToolContext) -> str:
    del context
    return """Replace an inclusive 1-based line range when exact positions and replacement are already known. Supply the snapshot_id from read/search to reject stale or unseen ranges; prefer hashline_edit for ordinary grounded edits."""


def _hashline_edit_description(context: McpToolContext) -> str:
    del context
    return """Default grounded edit for existing files. Copy fresh [path#snapshot_id] and line:text rows from read/search; add +final-content rows or use SWAP/INSERT directives as described in the input schema. Rejects stale snapshots and overlapping/unseen ranges."""


@file_tool(
    http_method="POST",
    http_path="/tools/list_files",
    description=_list_files_description,
    annotations="read_only",
    oauth_scopes=("shell:read",),
)
async def list_files(
    session_id: SessionIdArg,
    path: ListPathArg = ".",
    recursive: RecursiveArg = False,
    max_entries: MaxEntriesArg = 500,
) -> ListFilesOutput:
    """List files and directories under a session workdir path."""
    del session_id, path, recursive, max_entries
    raise RuntimeError("list_files requires control routing")


@file_tool(
    http_method="POST",
    http_path="/tools/write_file",
    description=_write_file_description,
    oauth_scopes=("shell:read", "shell:write"),
    timeout_cancellable=False,
)
async def write_file(
    session_id: SessionIdArg,
    path: FilePathArg,
    content: FileContentArg,
    overwrite: OverwriteArg = True,
) -> WriteFileOutput:
    """Write a UTF-8 text file through an execution session."""
    del session_id, path, content, overwrite
    raise RuntimeError("write_file requires control routing")


@file_tool(
    http_method="POST",
    http_path="/tools/edit_lines",
    description=_edit_lines_description,
    oauth_scopes=("shell:read", "shell:write"),
    timeout_cancellable=False,
)
async def edit_lines(
    path: FilePathArg,
    start_line: EditStartLineArg,
    end_line: EditEndLineArg,
    replacement: LineReplacementArg,
    session_id: SessionIdArg,
    snapshot_id: SnapshotIdArg = None,
) -> EditLinesOutput:
    """Replace an inclusive whole-line range in a file."""
    del path, start_line, end_line, replacement, session_id, snapshot_id
    raise RuntimeError("edit_lines requires control routing")


@file_tool(
    http_method="POST",
    http_path="/tools/hashline_edit",
    description=_hashline_edit_description,
    oauth_scopes=("shell:read", "shell:write"),
    timeout_cancellable=False,
)
async def hashline_edit(
    session_id: SessionIdArg,
    input: HashlineEditInputArg,
) -> HashlineEditOutput:
    """Apply compact hashline edits copied from read/search output."""
    del session_id, input
    raise RuntimeError("hashline_edit requires control routing")


@file_tool(
    http_method="POST",
    http_path="/tools/delete",
    description="Delete a file or directory through an execution session.",
    oauth_scopes=("shell:read", "shell:write"),
    timeout_cancellable=False,
)
async def delete_file_or_dir(
    session_id: SessionIdArg, path: FilePathArg, recursive: RecursiveArg = False
) -> DeleteFileOrDirOutput:
    """Delete a file or directory through an execution session."""
    del session_id, path, recursive
    raise RuntimeError("delete_file_or_dir requires control routing")
