"""Provide workspace-aware UTF-8 file operations with path containment and bounded output."""

import codecs
import contextlib
import difflib
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..config.settings import Settings
from ..schemas.result_models.files import (
    DeleteFileOrDirOutput,
    EditLinesOutput,
    EntryInfo,
    HashlineEditHunkOutput,
    HashlineEditOutput,
    LineRange,
    ListFilesOutput,
    ReadFileOutput,
    ReadLine,
    WriteFileOutput,
)
from ..utils.path_locks import path_lock, path_locks
from .path import (
    relative_display_from_root,
    resolve_path_with_policy,
)
from .tool_session.bindings import SessionBinding
from .tool_session.store import (
    ToolSessionStore,
    file_sha256,
)


@dataclass(frozen=True)
class FilesConfig:
    """Configuration values consumed by the Files domain."""

    workspace_root: Path
    """Workspace root used for sessionless paths and display normalization."""
    allow_full_control: bool
    """Whether filesystem access may escape the configured workspace root."""
    path_denylist: tuple[str, ...]
    """Denied path fragments enforced by the shared path policy."""
    max_directory_entries: int
    """Maximum directory entries returned by one list operation."""
    max_file_read_bytes: int
    """Maximum bytes decoded from one file read."""
    max_file_write_bytes: int
    """Maximum UTF-8 bytes accepted by file write and edit operations."""


def files_config_from_settings(settings: Settings) -> FilesConfig:
    """Project application settings to the values Files actually consumes."""
    return FilesConfig(
        workspace_root=settings.workspace_root,
        allow_full_control=settings.allow_full_control,
        path_denylist=tuple(settings.path_denylist),
        max_directory_entries=settings.max_directory_entries,
        max_file_read_bytes=settings.max_file_read_bytes,
        max_file_write_bytes=settings.max_file_write_bytes,
    )


def _resolve_file_path(
    config: FilesConfig,
    binding: SessionBinding | None,
    path: str | Path,
    *,
    must_exist: bool = False,
    allow_missing_parent: bool = True,
    follow_final_symlink: bool = True,
) -> Path:
    """Resolve a workspace or local-session path from explicit Files policy."""
    if binding is None:
        return resolve_path_with_policy(
            path,
            workspace_root=config.workspace_root,
            allow_full_control=config.allow_full_control,
            path_denylist=config.path_denylist,
            must_exist=must_exist,
            allow_missing_parent=allow_missing_parent,
            follow_final_symlink=follow_final_symlink,
        )

    workdir = Path(binding.workdir).resolve()
    raw = Path(path)
    candidate = raw if raw.is_absolute() else workdir / raw
    resolved = resolve_path_with_policy(
        candidate,
        workspace_root=config.workspace_root,
        allow_full_control=config.allow_full_control,
        path_denylist=config.path_denylist,
        must_exist=must_exist,
        allow_missing_parent=allow_missing_parent,
        follow_final_symlink=follow_final_symlink,
    )
    boundary = resolved if follow_final_symlink else resolved.parent
    try:
        boundary.relative_to(workdir)
    except ValueError as exc:
        raise ValueError(f"Path escapes session workdir: {path}") from exc
    return resolved


def _display_file(config: FilesConfig, path: Path) -> str:
    """Render a file path relative to the explicit workspace root."""
    return relative_display_from_root(path, config.workspace_root)


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace one user text file while preserving its existing mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = path.stat().st_mode & 0o777 if path.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if previous_mode is not None:
            temporary.chmod(previous_mode)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def _list_files_local(
    config: FilesConfig,
    binding: SessionBinding | None,
    path: str = ".",
    recursive: bool = False,
    max_entries: int = 500,
) -> ListFilesOutput:
    """List files from explicit Files policy and an optional local binding."""
    base = _resolve_file_path(config, binding, path, must_exist=True)
    if not base.is_dir():
        raise NotADirectoryError(str(base))
    filelist: list[EntryInfo] = []
    max_directory_entries = config.max_directory_entries
    if not (0 <= max_entries <= max_directory_entries):
        raise ValueError(
            f"max_entries must be between 0 and {max_directory_entries}"
        )
    limit = min(max_entries, max_directory_entries)
    iterator = base.rglob("*") if recursive else base.iterdir()

    truncated = False
    for item in iterator:
        if len(filelist) >= limit:
            truncated = True
            break
        try:
            stat = item.lstat()
            is_link = item.is_symlink()
        except OSError:
            continue
        entry_type = (
            "link"
            if is_link
            else "dir"
            if item.is_dir()
            else "file"
            if item.is_file()
            else "other"
        )
        target = None
        if is_link:
            with contextlib.suppress(OSError):
                target = os.readlink(item)
        filelist.append(
            EntryInfo(
                path=_display_file(config, item),
                type=entry_type,
                size=stat.st_size if entry_type in {"file", "link"} else None,
                modified=stat.st_mtime,
                target=target,
            )
        )
    return ListFilesOutput(
        limit_count=limit,
        count=len(filelist),
        is_truncated=truncated,
        entries=filelist,
    )


type _ReadLineRange = tuple[int | None, int | None]


def _selected_read_lines(
    lines: list[str],
    start_line: int | None,
    end_line: int | None,
) -> list[ReadLine]:
    """Return decoded lines with original line numbers for a requested range."""
    total_lines = len(lines)
    if total_lines == 0:
        return []
    start = max(1, start_line or 1)
    end = min(total_lines, end_line or total_lines)
    if end < start:
        return []
    return [
        ReadLine(line=line_number, text=lines[line_number - 1])
        for line_number in range(start, end + 1)
    ]


def _selected_read_lines_by_ranges(
    lines: list[str], ranges: Sequence[_ReadLineRange]
) -> tuple[list[ReadLine], tuple[tuple[int, int], ...]]:
    """Return decoded lines plus the exact non-empty ranges actually shown."""
    selected_lines: list[ReadLine] = []
    seen_ranges: list[tuple[int, int]] = []
    for start_line, end_line in ranges:
        range_lines = _selected_read_lines(lines, start_line, end_line)
        if not range_lines:
            continue
        selected_lines.extend(range_lines)
        seen_ranges.append((range_lines[0].line, range_lines[-1].line))
    return selected_lines, tuple(seen_ranges)


def _numbered_content(
    lines: list[ReadLine],
    path: str | None = None,
    snapshot_id: str | None = None,
) -> str:
    """Format lines in hashline-style grounded model-facing form."""
    body = "\n".join(f"{line.line}:{line.text}" for line in lines)
    if path is not None and snapshot_id is not None:
        header = "[" + path + "#" + snapshot_id + "]"
        return header + "\n" + body if body else header
    return body


def read_file_explicit(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    *,
    line_ranges: Sequence[_ReadLineRange] | None = None,
    max_file_read_bytes: int,
    resolve_file: Callable[[str], Path],
    display_file: Callable[[Path], str],
    snapshot_store: ToolSessionStore | None = None,
    snapshot_session_id: str | None = None,
) -> ReadFileOutput:
    """Read and optionally ground a file from explicit path and snapshot dependencies."""
    p = resolve_file(path)
    size = p.stat().st_size
    with p.open("rb") as fh:
        data = fh.read(max_file_read_bytes + 1)

    truncated = False
    if len(data) > max_file_read_bytes:
        data = data[:max_file_read_bytes]
        truncated = True
    truncated_bytes = max(0, size - len(data))
    decoder = codecs.getincrementaldecoder("utf-8")()
    text = decoder.decode(data, final=not truncated)
    all_lines = text.splitlines()
    total_lines = len(all_lines)
    range_specs = (
        tuple(line_ranges)
        if line_ranges is not None
        else ((start_line, end_line),)
    )
    selected_lines, seen_ranges = _selected_read_lines_by_ranges(
        all_lines, range_specs
    )
    if (
        line_ranges is not None
        or start_line is not None
        or end_line is not None
    ):
        text = "\n".join(line.text for line in selected_lines)

    start = selected_lines[0].line if selected_lines else None
    end = selected_lines[-1].line if selected_lines else None
    seen_range_models = [
        LineRange(start=range_start, end=range_end)
        for range_start, range_end in seen_ranges
    ]
    relative_path = display_file(p)
    record = (
        snapshot_store.record_file_snapshot(
            session_id=snapshot_session_id,
            path=relative_path,
            file_sha256=file_sha256(p),
            total_lines=total_lines,
            seen_ranges=seen_ranges,
        )
        if snapshot_store is not None and snapshot_session_id is not None
        else None
    )
    return ReadFileOutput(
        path=relative_path,
        bytes=size,
        bytes_read=len(data),
        truncated_bytes=truncated_bytes,
        total_lines=total_lines,
        start_line=start,
        end_line=end,
        line_count=len(selected_lines),
        lines=selected_lines,
        numbered_content=_numbered_content(
            selected_lines,
            relative_path,
            record.snapshot_id if record is not None else None,
        ),
        session_id=record.session_id if record is not None else None,
        snapshot_id=record.snapshot_id if record is not None else None,
        file_sha256=record.file_sha256 if record is not None else None,
        seen_ranges=seen_range_models if record is not None else [],
        truncated=truncated,
        content=text,
    )


def _read_file_local(
    config: FilesConfig,
    store: ToolSessionStore,
    binding: SessionBinding | None,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    line_ranges: Sequence[_ReadLineRange] | None = None,
) -> ReadFileOutput:
    """Read and ground a local file from explicit Files dependencies."""
    return read_file_explicit(
        path,
        start_line,
        end_line,
        line_ranges=line_ranges,
        max_file_read_bytes=config.max_file_read_bytes,
        resolve_file=lambda value: _resolve_file_path(
            config, binding, value, must_exist=True
        ),
        display_file=lambda value: _display_file(config, value),
        snapshot_store=store if binding is not None else None,
        snapshot_session_id=binding.session_id if binding is not None else None,
    )


def _write_file_local(
    config: FilesConfig,
    binding: SessionBinding | None,
    path: str,
    content: str,
    overwrite: bool = True,
    expected_sha256: str | None = None,
) -> WriteFileOutput:
    """Write local text from explicit Files policy and path binding."""
    data = content.encode("utf-8")
    if len(data) > config.max_file_write_bytes:
        raise ValueError(
            f"Refusing to write {len(data)} bytes; max is {config.max_file_write_bytes}"
        )
    if expected_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ):
        raise ValueError("expected_sha256 must be a lowercase SHA-256 digest")
    p = _resolve_file_path(config, binding, path)
    with path_lock(p):
        exists = p.exists()
        if exists and not overwrite:
            raise FileExistsError(str(p))
        if expected_sha256 is not None and (
            not exists or not p.is_file() or file_sha256(p) != expected_sha256
        ):
            raise ValueError("File changed; reload before saving")
        created = not exists
        _atomic_write_text(p, content)
    return WriteFileOutput(
        path=_display_file(config, p), bytes=len(data), created=created
    )


def _newline_for_text(text: str) -> str:
    """Return the dominant newline sequence for whole-line edits."""
    return "\r\n" if "\r\n" in text else "\n"


def _replacement_lines(
    replacement: str,
    *,
    newline: str,
    selected_had_trailing_newline: bool,
    has_following_lines: bool,
) -> list[str]:
    """Return replacement text as whole-line chunks with stable newlines."""
    if replacement == "":
        return []
    text = replacement
    if not text.endswith(("\n", "\r")) and (
        selected_had_trailing_newline or has_following_lines
    ):
        text = f"{text}{newline}"
    return text.splitlines(keepends=True)


def _range_is_visible(
    start_line: int, end_line: int, ranges: tuple[tuple[int, int], ...]
) -> bool:
    """Return whether an edit range is contained in displayed ranges."""
    return any(
        start_line >= visible_start and end_line <= visible_end
        for visible_start, visible_end in ranges
    )


_HASHLINE_HEADER_RE = re.compile(r"^\[(?P<path>.+)#(?P<snapshot>[^\]]+)\]$")
_HASHLINE_ROW_RE = re.compile(r"^(?P<line>\d+):(?P<text>.*)$")
_HASHLINE_SWAP_RE = re.compile(
    r"^SWAP\s+(?P<start>\d+)(?:-(?P<end>\d+))?:\s*$", re.IGNORECASE
)
_HASHLINE_INSERT_RE = re.compile(
    r"^INSERT(?:\s+(?P<where>BEFORE|AFTER))?\s+(?P<line>\d+):\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _ParsedHashlineEdit:
    """Parsed hashline replacement or deletion operation."""

    path: str
    snapshot_id: str
    start_line: int
    end_line: int
    replacement: str
    expected_lines: tuple[ReadLine, ...] = ()


@dataclass(frozen=True)
class _ParsedHashlineInsert:
    """Parsed hashline insertion anchored on a visible line."""

    path: str
    snapshot_id: str
    anchor_line: int
    insert_after: bool
    inserted_lines: tuple[str, ...]


type _ParsedHashlineOperation = _ParsedHashlineEdit | _ParsedHashlineInsert


@dataclass(frozen=True)
class _PreparedHashlineHunk:
    """One validated hashline hunk prepared for a file-level edit."""

    input_index: int
    path_obj: Path
    relative_path: str
    start_line: int
    end_line: int
    replacement_lines: tuple[str, ...]


@dataclass(frozen=True)
class _HashlineFileSnapshot:
    """Original file state shared by all hunks targeting one file."""

    path_obj: Path
    relative_path: str
    original: str
    original_lines: tuple[str, ...]
    current_sha256: str


def _hashline_replacement_text(lines: Sequence[str]) -> str:
    """Convert plus-prefixed hashline payload rows into edit_lines replacement text."""
    if not lines:
        return ""
    text = "\n".join(lines)
    if lines[-1] == "":
        text += "\n"
    return text


def _hashline_plus_lines(
    lines: Sequence[str], *, require: bool
) -> tuple[str, ...]:
    """Return replacement payload lines after stripping one leading '+'."""
    if require and not lines:
        raise ValueError(
            "hashline edit requires at least one + replacement line"
        )
    for line in lines:
        if not line.startswith("+"):
            raise ValueError("hashline replacement lines must start with '+'")
    return tuple(line[1:] for line in lines)


def _parse_hashline_hunk(
    path: str, snapshot_id: str, body: Sequence[str]
) -> _ParsedHashlineOperation:
    """Parse one non-empty hashline hunk body under a header."""
    if not body:
        raise ValueError("hashline edit input must include an edit hunk")

    swap_match = _HASHLINE_SWAP_RE.match(body[0].strip())
    if swap_match is not None:
        start_line = int(swap_match.group("start"))
        end_line = int(swap_match.group("end") or start_line)
        if end_line < start_line:
            raise ValueError("SWAP end line must be >= start line")
        plus_lines = _hashline_plus_lines(body[1:], require=False)
        return _ParsedHashlineEdit(
            path=path,
            snapshot_id=snapshot_id,
            start_line=start_line,
            end_line=end_line,
            replacement=_hashline_replacement_text(plus_lines),
        )

    insert_match = _HASHLINE_INSERT_RE.match(body[0].strip())
    if insert_match is not None:
        plus_lines = _hashline_plus_lines(body[1:], require=True)
        where = (insert_match.group("where") or "BEFORE").upper()
        return _ParsedHashlineInsert(
            path=path,
            snapshot_id=snapshot_id,
            anchor_line=int(insert_match.group("line")),
            insert_after=where == "AFTER",
            inserted_lines=plus_lines,
        )

    old_lines: list[ReadLine] = []
    replacement_start = len(body)
    for index, line in enumerate(body):
        if line.startswith("+"):
            replacement_start = index
            break
        row_match = _HASHLINE_ROW_RE.match(line)
        if row_match is None:
            raise ValueError(
                "hashline old lines must use '<line>:<text>' rows copied from read/search output"
            )
        old_lines.append(
            ReadLine(
                line=int(row_match.group("line")),
                text=row_match.group("text"),
            )
        )
    if not old_lines:
        raise ValueError("hashline edit must include old lines or a directive")
    expected_line = old_lines[0].line
    for old_line in old_lines:
        if old_line.line != expected_line:
            raise ValueError("hashline old lines must be consecutive")
        expected_line += 1
    plus_lines = _hashline_plus_lines(body[replacement_start:], require=False)
    return _ParsedHashlineEdit(
        path=path,
        snapshot_id=snapshot_id,
        start_line=old_lines[0].line,
        end_line=old_lines[-1].line,
        replacement=_hashline_replacement_text(plus_lines),
        expected_lines=tuple(old_lines),
    )


def parse_hashline_edit_input(
    input_text: str,
) -> tuple[_ParsedHashlineOperation, ...]:
    """Parse the compact hashline edit format accepted by hashline_edit."""
    lines = [line.rstrip("\r") for line in input_text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise ValueError("hashline edit input is empty")

    operations: list[_ParsedHashlineOperation] = []
    current_path: str | None = None
    current_snapshot_id: str | None = None
    current_body: list[str] = []

    def flush_hunk() -> None:
        nonlocal current_body
        if current_path is None or current_snapshot_id is None:
            if current_body:
                raise ValueError(
                    "hashline edit input must start with [path#snapshot_id]"
                )
            return
        if not current_body:
            return
        operations.append(
            _parse_hashline_hunk(
                current_path, current_snapshot_id, current_body
            )
        )
        current_body = []

    for line in lines:
        stripped = line.strip()
        header_match = _HASHLINE_HEADER_RE.match(stripped)
        if header_match is not None:
            flush_hunk()
            current_path = header_match.group("path")
            current_snapshot_id = header_match.group("snapshot")
            continue
        if current_path is None or current_snapshot_id is None:
            raise ValueError(
                "hashline edit input must start with [path#snapshot_id]"
            )
        if not stripped:
            flush_hunk()
            continue
        current_body.append(line)

    flush_hunk()
    if not operations:
        raise ValueError("hashline edit input must include an edit hunk")
    return tuple(operations)


def _path_for_hashline_operation(
    config: FilesConfig,
    store: ToolSessionStore,
    binding: SessionBinding | None,
    path: str,
    snapshot_id: str,
) -> str:
    """Return a path usable by edit_lines, preserving copied read/search headers."""
    if binding is None:
        return path
    record = store.get_snapshot(binding.session_id, snapshot_id)
    if record is None or record.path != path:
        return path
    candidate = config.workspace_root / record.path
    if candidate.exists():
        return str(candidate)
    return path


def _validate_snapshot_for_edit(
    *,
    store: ToolSessionStore,
    binding: SessionBinding | None,
    path: str,
    current_sha256: str,
    start_line: int,
    end_line: int,
    snapshot_id: str | None,
) -> None:
    """Validate optional snapshot freshness and visible-range grounding."""
    if snapshot_id is None:
        return
    if binding is None:
        raise ValueError("session_id is required when snapshot_id is provided")
    record = store.get_snapshot(binding.session_id, snapshot_id)
    if record is None:
        raise ValueError(
            "snapshot_id not found for this session; re-read the file"
        )
    if record.path != path:
        raise ValueError(
            "snapshot_id belongs to a different file; re-read the target file"
        )
    if record.file_sha256 != current_sha256:
        raise ValueError("file changed since snapshot; re-read before editing")
    if record.seen_ranges and not _range_is_visible(
        start_line, end_line, record.seen_ranges
    ):
        raise ValueError(
            "edit range was not shown by the referenced snapshot; re-read the target lines"
        )


def _hashline_file_snapshot(
    config: FilesConfig,
    binding: SessionBinding | None,
    path: str,
) -> _HashlineFileSnapshot:
    """Resolve a hashline path and capture current file state."""
    p = _resolve_file_path(config, binding, path, must_exist=True)
    size = p.stat().st_size
    if size > config.max_file_write_bytes:
        raise ValueError(
            f"Refusing to edit {size} bytes; max is {config.max_file_write_bytes}"
        )
    original = p.read_text(encoding="utf-8")
    return _HashlineFileSnapshot(
        path_obj=p,
        relative_path=_display_file(config, p),
        original=original,
        original_lines=tuple(original.splitlines(keepends=True)),
        current_sha256=file_sha256(p),
    )


def _hashline_operation_range(
    operation: _ParsedHashlineOperation,
) -> tuple[int, int]:
    """Return the original inclusive line range touched by an operation."""
    if isinstance(operation, _ParsedHashlineInsert):
        return operation.anchor_line, operation.anchor_line
    return operation.start_line, operation.end_line


def _prepare_hashline_hunk(
    *,
    config: FilesConfig,
    store: ToolSessionStore,
    binding: SessionBinding | None,
    input_index: int,
    operation: _ParsedHashlineOperation,
    snapshots: dict[Path, _HashlineFileSnapshot],
) -> _PreparedHashlineHunk:
    """Validate one parsed hashline hunk and convert it to replacement lines."""
    path_arg = _path_for_hashline_operation(
        config, store, binding, operation.path, operation.snapshot_id
    )
    snapshot = _hashline_file_snapshot(config, binding, path_arg)
    snapshots.setdefault(snapshot.path_obj, snapshot)
    snapshot = snapshots[snapshot.path_obj]

    start_line, end_line = _hashline_operation_range(operation)
    if start_line < 1:
        raise ValueError("line numbers must be >= 1")
    if end_line < start_line:
        raise ValueError("end_line must be >= start_line")

    total_lines = len(snapshot.original_lines)
    if end_line > total_lines:
        raise ValueError(
            f"line {end_line} is beyond file line count {total_lines}"
        )

    _validate_snapshot_for_edit(
        store=store,
        binding=binding,
        path=snapshot.relative_path,
        current_sha256=snapshot.current_sha256,
        start_line=start_line,
        end_line=end_line,
        snapshot_id=operation.snapshot_id,
    )

    decoded_lines = snapshot.original.splitlines()
    if isinstance(operation, _ParsedHashlineInsert):
        anchor_text = decoded_lines[operation.anchor_line - 1]
        if operation.insert_after:
            replacement_text = _hashline_replacement_text(
                (anchor_text, *operation.inserted_lines)
            )
        else:
            replacement_text = _hashline_replacement_text(
                (*operation.inserted_lines, anchor_text)
            )
    else:
        if operation.expected_lines:
            expected = [line.text for line in operation.expected_lines]
            current = decoded_lines[start_line - 1 : end_line]
            if current != expected:
                raise ValueError(
                    "hashline old text does not match current file; re-read before editing"
                )
        replacement_text = operation.replacement

    selected = snapshot.original_lines[start_line - 1 : end_line]
    replacement_lines = _replacement_lines(
        replacement_text,
        newline=_newline_for_text(snapshot.original),
        selected_had_trailing_newline=bool(
            selected and selected[-1].endswith(("\n", "\r"))
        ),
        has_following_lines=end_line < total_lines,
    )
    return _PreparedHashlineHunk(
        input_index=input_index,
        path_obj=snapshot.path_obj,
        relative_path=snapshot.relative_path,
        start_line=start_line,
        end_line=end_line,
        replacement_lines=tuple(replacement_lines),
    )


def _validate_hashline_hunk_overlap(
    hunks: Sequence[_PreparedHashlineHunk],
) -> None:
    """Reject multiple hunks that touch overlapping original line ranges."""
    by_file: dict[Path, list[_PreparedHashlineHunk]] = {}
    for hunk in hunks:
        by_file.setdefault(hunk.path_obj, []).append(hunk)
    for file_hunks in by_file.values():
        previous: _PreparedHashlineHunk | None = None
        for hunk in sorted(file_hunks, key=lambda item: item.start_line):
            if previous is not None and hunk.start_line <= previous.end_line:
                raise ValueError(
                    "hashline edit hunks overlap; use non-overlapping original line ranges"
                )
            previous = hunk


def _apply_hashline_file_hunks(
    config: FilesConfig,
    snapshot: _HashlineFileSnapshot,
    hunks: Sequence[_PreparedHashlineHunk],
) -> tuple[tuple[str, ...], str]:
    """Apply prepared hunks to one file and return updated lines plus diff."""
    updated_lines = list(snapshot.original_lines)
    for hunk in sorted(hunks, key=lambda item: item.start_line, reverse=True):
        updated_lines = (
            updated_lines[: hunk.start_line - 1]
            + list(hunk.replacement_lines)
            + updated_lines[hunk.end_line :]
        )
    updated = "".join(updated_lines)
    updated_bytes = len(updated.encode("utf-8"))
    if updated_bytes > config.max_file_write_bytes:
        raise ValueError(
            f"Refusing to write {updated_bytes} bytes; max is {config.max_file_write_bytes}"
        )

    diff = "".join(
        difflib.unified_diff(
            snapshot.original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=snapshot.relative_path,
            tofile=snapshot.relative_path,
        )
    )
    _atomic_write_text(snapshot.path_obj, updated)
    return tuple(updated_lines), diff


def _hashline_hunk_contexts(
    *,
    config: FilesConfig,
    store: ToolSessionStore,
    binding: SessionBinding | None,
    prepared_hunks: Sequence[_PreparedHashlineHunk],
    updated_by_file: dict[Path, tuple[str, ...]],
    snapshots: dict[Path, _HashlineFileSnapshot],
) -> list[HashlineEditHunkOutput]:
    """Build fresh post-edit context for each hunk in original input order."""
    outputs_by_index: dict[int, HashlineEditHunkOutput] = {}
    by_file: dict[Path, list[_PreparedHashlineHunk]] = {}
    for hunk in prepared_hunks:
        by_file.setdefault(hunk.path_obj, []).append(hunk)

    for path_obj, file_hunks in by_file.items():
        delta = 0
        updated_lines = updated_by_file[path_obj]
        snapshot = snapshots[path_obj]
        for hunk in sorted(file_hunks, key=lambda item: item.start_line):
            new_start = max(1, hunk.start_line + delta)
            replacement_line_count = len(hunk.replacement_lines)
            context_start = max(1, new_start - 3)
            context_end = min(
                len(updated_lines),
                max(
                    new_start,
                    new_start + max(replacement_line_count, 1) + 3,
                ),
            )
            if not updated_lines:
                context_start = context_end = 1
            context = _read_file_local(
                config,
                store,
                binding,
                str(snapshot.path_obj),
                context_start,
                context_end,
            )
            outputs_by_index[hunk.input_index] = HashlineEditHunkOutput(
                path=hunk.relative_path,
                start_line=hunk.start_line,
                end_line=hunk.end_line,
                replacement_line_count=replacement_line_count,
                context=context,
            )
            delta += replacement_line_count - (
                hunk.end_line - hunk.start_line + 1
            )

    return [
        outputs_by_index[hunk.input_index]
        for hunk in sorted(prepared_hunks, key=lambda item: item.input_index)
    ]


def _hashline_operation_paths(
    config: FilesConfig,
    store: ToolSessionStore,
    binding: SessionBinding | None,
    operations: Sequence[_ParsedHashlineOperation],
) -> list[Path]:
    """Resolve hashline targets before acquiring their shared mutation locks."""
    paths: list[Path] = []
    for operation in operations:
        path_arg = _path_for_hashline_operation(
            config, store, binding, operation.path, operation.snapshot_id
        )
        resolved = _resolve_file_path(
            config, binding, path_arg, must_exist=True
        )
        paths.append(resolved)
    return paths


def _hashline_edit_locked(
    config: FilesConfig,
    store: ToolSessionStore,
    binding: SessionBinding | None,
    operations: Sequence[_ParsedHashlineOperation],
) -> HashlineEditOutput:
    """Validate and apply hashline operations while all target locks are held."""
    snapshots: dict[Path, _HashlineFileSnapshot] = {}
    prepared_hunks = [
        _prepare_hashline_hunk(
            config=config,
            store=store,
            binding=binding,
            input_index=index,
            operation=operation,
            snapshots=snapshots,
        )
        for index, operation in enumerate(operations)
    ]
    _validate_hashline_hunk_overlap(prepared_hunks)

    hunks_by_file: dict[Path, list[_PreparedHashlineHunk]] = {}
    for hunk in prepared_hunks:
        hunks_by_file.setdefault(hunk.path_obj, []).append(hunk)

    updated_by_file: dict[Path, tuple[str, ...]] = {}
    diff_parts: list[str] = []
    for path_obj, file_hunks in hunks_by_file.items():
        updated_lines, diff = _apply_hashline_file_hunks(
            config, snapshots[path_obj], file_hunks
        )
        updated_by_file[path_obj] = updated_lines
        diff_parts.append(diff)

    hunk_outputs = _hashline_hunk_contexts(
        config=config,
        store=store,
        binding=binding,
        prepared_hunks=prepared_hunks,
        updated_by_file=updated_by_file,
        snapshots=snapshots,
    )
    first_hunk = hunk_outputs[0]
    first_path = first_hunk.path
    if all(hunk.path == first_path for hunk in hunk_outputs):
        start_line = min(hunk.start_line for hunk in hunk_outputs)
        end_line = max(hunk.end_line for hunk in hunk_outputs)
    else:
        start_line = first_hunk.start_line
        end_line = first_hunk.end_line
    return HashlineEditOutput(
        path=first_path,
        start_line=start_line,
        end_line=end_line,
        replacement_line_count=sum(
            hunk.replacement_line_count for hunk in hunk_outputs
        ),
        diff="".join(diff_parts),
        context=first_hunk.context,
        hunk_count=len(hunk_outputs),
        hunks=hunk_outputs,
    )


def _hashline_edit_local(
    config: FilesConfig,
    store: ToolSessionStore,
    binding: SessionBinding | None,
    input_text: str,
) -> HashlineEditOutput:
    """Apply hashline edits from explicit Files dependencies."""
    operations = parse_hashline_edit_input(input_text)
    with path_locks(
        _hashline_operation_paths(config, store, binding, operations)
    ):
        return _hashline_edit_locked(config, store, binding, operations)


def _edit_lines_local(
    config: FilesConfig,
    store: ToolSessionStore,
    binding: SessionBinding | None,
    path: str,
    start_line: int,
    end_line: int,
    replacement: str,
    snapshot_id: str | None = None,
) -> EditLinesOutput:
    """Replace a local line range from explicit Files dependencies."""
    if start_line < 1:
        raise ValueError("start_line must be >= 1")
    if end_line < start_line:
        raise ValueError("end_line must be >= start_line")

    p = _resolve_file_path(config, binding, path, must_exist=True)
    with path_lock(p):
        size = p.stat().st_size
        if size > config.max_file_write_bytes:
            raise ValueError(
                f"Refusing to edit {size} bytes; max is {config.max_file_write_bytes}"
            )

        relative_path = _display_file(config, p)
        current_sha256 = file_sha256(p)
        _validate_snapshot_for_edit(
            store=store,
            binding=binding,
            path=relative_path,
            current_sha256=current_sha256,
            start_line=start_line,
            end_line=end_line,
            snapshot_id=snapshot_id,
        )

        original = p.read_text(encoding="utf-8")
        original_lines = original.splitlines(keepends=True)
        total_lines = len(original_lines)
        if end_line > total_lines:
            raise ValueError(
                f"end_line {end_line} is beyond file line count {total_lines}"
            )

        selected = original_lines[start_line - 1 : end_line]
        replacement_lines = _replacement_lines(
            replacement,
            newline=_newline_for_text(original),
            selected_had_trailing_newline=bool(
                selected and selected[-1].endswith(("\n", "\r"))
            ),
            has_following_lines=end_line < total_lines,
        )
        updated_lines = (
            original_lines[: start_line - 1]
            + replacement_lines
            + original_lines[end_line:]
        )
        updated = "".join(updated_lines)
        updated_bytes = len(updated.encode("utf-8"))
        if updated_bytes > config.max_file_write_bytes:
            raise ValueError(
                f"Refusing to write {updated_bytes} bytes; max is {config.max_file_write_bytes}"
            )

        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=relative_path,
                tofile=relative_path,
            )
        )
        _atomic_write_text(p, updated)

        replacement_line_count = len(replacement_lines)
        context_start = max(1, start_line - 3)
        context_end = min(
            len(updated_lines),
            max(
                start_line,
                start_line + max(replacement_line_count, 1) + 3,
            ),
        )
        context = _read_file_local(
            config, store, binding, str(p), context_start, context_end
        )
    return EditLinesOutput(
        path=relative_path,
        start_line=start_line,
        end_line=end_line,
        replacement_line_count=replacement_line_count,
        diff=diff,
        context=context,
    )


def _delete_file_or_dir_local(
    config: FilesConfig,
    binding: SessionBinding | None,
    path: str,
    recursive: bool = False,
) -> DeleteFileOrDirOutput:
    """Delete a local path from explicit Files policy and binding."""
    p = _resolve_file_path(
        config,
        binding,
        path,
        must_exist=True,
        follow_final_symlink=False,
    )
    with path_lock(p):
        if not os.path.lexists(p):
            raise FileNotFoundError(str(p))
        if p.is_symlink():
            p.unlink()
            deleted = "link"
        elif p.is_dir():
            if not recursive:
                raise IsADirectoryError(
                    "Set recursive=true to delete a directory"
                )
            shutil.rmtree(p)
            deleted = "directory"
        else:
            p.unlink()
            deleted = "file"
    return DeleteFileOrDirOutput(path=_display_file(config, p), deleted=deleted)
