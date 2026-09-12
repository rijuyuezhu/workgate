"""File operation tool registry."""

from __future__ import annotations

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
    return """List files and directories under a session workdir path for quick inspection. Relative paths resolve inside the explicit agent/workspace session. The result reports whether entries were truncated by the requested limit or server cap. The bound executor applies its configured directory-entry limit."""


def _write_file_description(context: McpToolContext) -> str:
    del context
    return """Write a complete UTF-8 file inside an explicit agent/workspace session. Use only for new files or intentional whole-file replacement; do not use it for partial edits. For ordinary edits to existing files, use hashline_edit from copied read/search rows instead of rewriting the file. Use edit_lines only when you already have exact structured path/start/end/replacement data. Use bash only when a command-driven transformation is clearer. The bound executor applies its configured write limit."""


def _edit_lines_description(context: McpToolContext) -> str:
    del context
    return """Low-level structured line edit for callers that already have exact path/start_line/end_line/replacement data. Do not use this as the normal model editing path from read/search output; use hashline_edit for copied `[path#snapshot_id]` plus `line:text` rows. If you do call edit_lines, pass the same session_id and the snapshot_id from the read/search result so stale files or unseen ranges are rejected. The range is inclusive, 1-based, and should cover only lines being changed; use an empty replacement to delete. The bound executor applies its configured write limit."""


def _hashline_edit_description(context: McpToolContext) -> str:
    del context
    return """Default model-facing edit tool for existing UTF-8 files. Copy the `[path#snapshot_id]` header and relevant `line:text` rows from the latest read/search output; never invent snapshot ids/tags. Then provide the final new content as `+text` rows. Supported hunk forms: copied rows followed by `+replacement` rows; copied rows with no `+` rows to delete; `SWAP start[-end]:` followed by `+replacement` rows; and `INSERT [BEFORE|AFTER] line:` followed by `+inserted` rows. To apply multiple non-overlapping hunks, separate hunk bodies with a blank line under the same header or repeat a `[path#snapshot_id]` header for another section or file. Body rows are final content only: use `+` for blank lines, preserve indentation after `+`, and do not write `-old` rows or bare context lines. Keep hunks tight. Line numbers refer to the original displayed snapshot; stale files, wrong paths, overlapping hunks, or unseen ranges are rejected. After every edit, use the returned fresh hunk contexts or run read/search again before the next edit. The bound executor applies its configured write limit."""


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
    """Write a UTF-8 text file inside a session workdir."""
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
    description="Delete a file or directory inside a session workdir.",
    oauth_scopes=("shell:read", "shell:write"),
    timeout_cancellable=False,
)
async def delete_file_or_dir(
    session_id: SessionIdArg, path: FilePathArg, recursive: RecursiveArg = False
) -> DeleteFileOrDirOutput:
    """Delete a file or directory inside a session workdir."""
    del session_id, path, recursive
    raise RuntimeError("delete_file_or_dir requires control routing")
