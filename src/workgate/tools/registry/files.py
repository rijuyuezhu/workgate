"""File operation tool registry."""

from dataclasses import replace

from ...config.settings import Settings
from ...ops.files import (
    delete_file_or_dir_dispatch_execute,
    edit_lines_dispatch_execute,
    hashline_edit_dispatch_execute,
    list_files_dispatch_execute,
    write_file_dispatch_execute,
)
from ...ops.files_service import FilesService
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
    """Register file operation tools."""

    name = "file"
    """Registry group name used for tool-surface organization."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        files_service: FilesService | None = None,
    ) -> None:
        super().__init__(settings)
        self._files_service = files_service

    def _require_service(self) -> FilesService:
        if self._files_service is None:
            raise RuntimeError("Files service is not bound")
        return self._files_service

    async def _bound_list_files(
        self,
        session_id: SessionIdArg,
        path: ListPathArg = ".",
        recursive: RecursiveArg = False,
        max_entries: MaxEntriesArg = 500,
    ) -> ListFilesOutput:
        return await self._require_service().list_files(
            session_id, path, recursive, max_entries
        )

    async def _bound_write_file(
        self,
        session_id: SessionIdArg,
        path: FilePathArg,
        content: FileContentArg,
        overwrite: OverwriteArg = True,
    ) -> WriteFileOutput:
        return await self._require_service().write_file(
            session_id, path, content, overwrite
        )

    async def _bound_edit_lines(
        self,
        path: FilePathArg,
        start_line: EditStartLineArg,
        end_line: EditEndLineArg,
        replacement: LineReplacementArg,
        session_id: SessionIdArg,
        snapshot_id: SnapshotIdArg = None,
    ) -> EditLinesOutput:
        return await self._require_service().edit_lines(
            session_id,
            path,
            start_line,
            end_line,
            replacement,
            snapshot_id,
        )

    async def _bound_hashline_edit(
        self,
        session_id: SessionIdArg,
        input: HashlineEditInputArg,
    ) -> HashlineEditOutput:
        return await self._require_service().hashline_edit(session_id, input)

    async def _bound_delete_file_or_dir(
        self,
        session_id: SessionIdArg,
        path: FilePathArg,
        recursive: RecursiveArg = False,
    ) -> DeleteFileOrDirOutput:
        return await self._require_service().delete_file_or_dir(
            session_id, path, recursive
        )

    def _enabled_tools(self):
        tools = super()._enabled_tools()
        if self._files_service is None:
            return tools
        bound = {
            "list_files": self._bound_list_files,
            "write_file": self._bound_write_file,
            "edit_lines": self._bound_edit_lines,
            "hashline_edit": self._bound_hashline_edit,
            "delete_file_or_dir": self._bound_delete_file_or_dir,
        }
        return tuple(
            replace(
                tool,
                func=bound[tool.name],
                session_admission="handler",
            )
            for tool in tools
        )


file_tool = FileToolRegistry.get_tool_decorator()


def _list_files_description(context: McpToolContext) -> str:
    settings = context.settings
    return f"""List files and directories under a session workdir path for quick inspection. Relative paths resolve inside the explicit agent/workspace session. The result reports whether entries were truncated by the requested limit or server cap. Current max directory entries: {settings.max_directory_entries}."""


def _write_file_description(context: McpToolContext) -> str:
    settings = context.settings
    return f"""Write a complete UTF-8 file inside an explicit agent/workspace session. Use only for new files or intentional whole-file replacement; do not use it for partial edits. For ordinary edits to existing files, use hashline_edit from copied read/search rows instead of rewriting the file. Use edit_lines only when you already have exact structured path/start/end/replacement data. Use bash only when a command-driven transformation is clearer. Current write cap: {settings.max_file_write_bytes} bytes."""


def _edit_lines_description(context: McpToolContext) -> str:
    settings = context.settings
    return f"""Low-level structured line edit for callers that already have exact path/start_line/end_line/replacement data. Do not use this as the normal model editing path from read/search output; use hashline_edit for copied `[path#snapshot_id]` plus `line:text` rows. If you do call edit_lines, pass the same session_id and the snapshot_id from the read/search result so stale files or unseen ranges are rejected. The range is inclusive, 1-based, and should cover only lines being changed; use an empty replacement to delete. Current write cap: {settings.max_file_write_bytes} bytes."""


def _hashline_edit_description(context: McpToolContext) -> str:
    settings = context.settings
    return f"""Default model-facing edit tool for existing UTF-8 files. Copy the `[path#snapshot_id]` header and relevant `line:text` rows from the latest read/search output; never invent snapshot ids/tags. Then provide the final new content as `+text` rows. Supported hunk forms: copied rows followed by `+replacement` rows; copied rows with no `+` rows to delete; `SWAP start[-end]:` followed by `+replacement` rows; and `INSERT [BEFORE|AFTER] line:` followed by `+inserted` rows. To apply multiple non-overlapping hunks, separate hunk bodies with a blank line under the same header or repeat a `[path#snapshot_id]` header for another section or file. Body rows are final content only: use `+` for blank lines, preserve indentation after `+`, and do not write `-old` rows or bare context lines. Keep hunks tight. Line numbers refer to the original displayed snapshot; stale files, wrong paths, overlapping hunks, or unseen ranges are rejected. After every edit, use the returned fresh hunk contexts or run read/search again before the next edit. Current write cap: {settings.max_file_write_bytes} bytes."""


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
    return await list_files_dispatch_execute(
        path, recursive, max_entries, session_id
    )


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
    return await write_file_dispatch_execute(
        path, content, overwrite, session_id
    )


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
    return await edit_lines_dispatch_execute(
        path,
        start_line,
        end_line,
        replacement,
        snapshot_id,
        session_id,
    )


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
    return await hashline_edit_dispatch_execute(input, session_id)


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
    return await delete_file_or_dir_dispatch_execute(
        path, recursive, session_id
    )
