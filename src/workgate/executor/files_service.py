"""Explicit operation-level orchestration for the Files vertical."""

from ..schemas.result_models.files import (
    DeleteFileOrDirOutput,
    EditLinesOutput,
    HashlineEditOutput,
    ListFilesOutput,
    ReadFileMetadata,
    ReadFileOutput,
    WriteFileOutput,
)
from ..schemas.result_models.read import ReadOutput
from .files import (
    FilesConfig,
    _delete_file_or_dir_local,
    _edit_lines_local,
    _hashline_edit_local,
    _list_files_local,
    _read_file_local,
    _write_file_local,
)
from .tool_session.bindings import SessionBinding
from .tool_session.resolver import SessionResolver
from .tool_session.selectors import parse_read_target
from .tool_session.store import ToolSessionStore


class FilesService:
    """Execute one Files operation from a fresh executor session binding."""

    def __init__(
        self,
        config: FilesConfig,
        store: ToolSessionStore,
    ) -> None:
        self.config = config
        self.store = store
        self.sessions = SessionResolver(store)

    def _binding(self, session_id: str) -> SessionBinding:
        return self.sessions.resolve_active_binding(session_id)

    async def list_files(
        self,
        session_id: str,
        path: str = ".",
        recursive: bool = False,
        max_entries: int = 500,
    ) -> ListFilesOutput:
        """List a directory from one admitted executor binding."""
        binding = self._binding(session_id)
        return _list_files_local(
            self.config, binding, path, recursive, max_entries
        )

    async def write_file(
        self,
        session_id: str,
        path: str,
        content: str,
        overwrite: bool = True,
        expected_sha256: str | None = None,
    ) -> WriteFileOutput:
        """Write a file from one admitted executor binding."""
        binding = self._binding(session_id)
        return _write_file_local(
            self.config,
            binding,
            path,
            content,
            overwrite,
            expected_sha256,
        )

    async def edit_lines(
        self,
        session_id: str,
        path: str,
        start_line: int,
        end_line: int,
        replacement: str,
        snapshot_id: str | None = None,
    ) -> EditLinesOutput:
        """Edit a grounded line range from one admitted binding."""
        binding = self._binding(session_id)
        return _edit_lines_local(
            self.config,
            self.store,
            binding,
            path,
            start_line,
            end_line,
            replacement,
            snapshot_id,
        )

    async def hashline_edit(
        self, session_id: str, input_text: str
    ) -> HashlineEditOutput:
        """Apply grounded hashline edits from one admitted binding."""
        binding = self._binding(session_id)
        return _hashline_edit_local(
            self.config, self.store, binding, input_text
        )

    async def delete_file_or_dir(
        self,
        session_id: str,
        path: str,
        recursive: bool = False,
    ) -> DeleteFileOrDirOutput:
        """Delete a file or directory from one admitted binding."""
        binding = self._binding(session_id)
        return _delete_file_or_dir_local(self.config, binding, path, recursive)

    async def read(self, session_id: str, path: str) -> ReadOutput:
        """Read a file or directory with selector semantics from one binding."""
        binding = self._binding(session_id)
        target = parse_read_target(path)
        listed: ListFilesOutput | None = None
        if not target.raw and not target.line_ranges:
            try:
                listed = _list_files_local(
                    self.config, binding, target.path, False, 500
                )
            except NotADirectoryError:
                listed = None
        if listed is not None:
            content_lines = [
                f"{entry.type}\t{entry.path}" for entry in listed.entries
            ]
            if listed.is_truncated:
                content_lines.append("[listing truncated]")
            return ReadOutput(
                kind="directory",
                path=target.path,
                raw=target.raw,
                content="\n".join(content_lines),
                directory=listed,
            )

        file_result = _read_file_local(
            self.config,
            self.store,
            binding,
            target.path,
            target.start_line,
            target.end_line,
            target.line_ranges or None,
        )
        return ReadOutput(
            kind="file",
            path=file_result.path,
            raw=target.raw,
            content=(
                file_result.content
                if target.raw
                else file_result.numbered_content
            ),
            file=ReadFileMetadata.from_read_result(file_result),
        )

    async def read_file(self, session_id: str, path: str) -> ReadFileOutput:
        """Read one UTF-8 file from an admitted executor session."""
        binding = self._binding(session_id)
        return _read_file_local(self.config, self.store, binding, path)
