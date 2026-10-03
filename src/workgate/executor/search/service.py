"""Executor-local application service for content Search."""

import asyncio
import fnmatch
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ...config.executor import resolve_default_workdir
from ...schemas.result_models.search import (
    GlobSearchOutput,
    GrepDisplayLine,
    GrepMatch,
    GrepSearchOutput,
    TreeViewOutput,
)
from ..files import read_file_explicit
from ..path import (
    display_path,
    resolve_path,
)
from ..tool_session.bindings import SessionBinding
from ..tool_session.resolver import SessionResolver
from ..tool_session.store import ToolSessionStore
from .core import (
    LineRanges,
    SearchConfig,
    SearchPaths,
    SearchWindowMatch,
    build_rg_argv,
    clamp_search_result_limit,
    display_windows,
    line_in_ranges,
    looks_like_glob,
    merge_line_ranges,
    parse_rg_match_record,
    search_match_path,
    search_path_items,
    split_line_scoped_search_path,
)

_SEARCH_CONTEXT_RADIUS = 1


@dataclass(frozen=True)
class SearchRequest:
    """Typed application request for executor-local Search."""

    pattern: str
    """Text or regular-expression pattern to search for."""
    paths: SearchPaths = None
    """Optional path, glob, or line-scoped path restrictions."""
    regex: bool = True
    """Interpret pattern as a regular expression when true."""
    case_sensitive: bool = True
    """Match case exactly when true."""
    max_results: int | None = None
    """Optional requested match limit before configured clamping."""
    skip: int = 0
    """Number of earlier actual matches to skip."""
    gitignore: bool = True
    """Respect gitignore-style exclusions when true."""


def _base(default_workdir: Path, workdir: str | None = None) -> Path:
    return (
        resolve_default_workdir(default_workdir)
        if workdir is None
        else Path(workdir)
    )


def _resolve(
    default_workdir: Path,
    path: str | Path,
    *,
    workdir: str | None = None,
    must_exist: bool = False,
    allow_missing_parent: bool = True,
    follow_final_symlink: bool = True,
) -> Path:
    return resolve_path(
        path,
        base=_base(default_workdir, workdir),
        must_exist=must_exist,
        allow_missing_parent=allow_missing_parent,
        follow_final_symlink=follow_final_symlink,
    )


def _display(
    default_workdir: Path,
    path: Path,
    *,
    workdir: str | None = None,
) -> str:
    return display_path(path, _base(default_workdir, workdir))


@dataclass(frozen=True)
class SearchGrounding:
    """Explicit file-read and snapshot capability for Search result grounding."""

    store: ToolSessionStore
    """Authoritative session store used only for snapshot recording."""
    default_workdir: Path
    """Configured executor default for sessionless path resolution."""
    max_file_read_bytes: int
    """Maximum bytes decoded by one grounding read."""

    def read(
        self,
        path: str,
        start_line: int,
        end_line: int,
        *,
        binding: SessionBinding | None,
    ):
        """Read one grounded window without reacquiring settings or session stores."""

        def resolve_file(path_value: str) -> Path:
            if binding is not None:
                return _resolve(
                    self.default_workdir,
                    path_value,
                    workdir=binding.workdir,
                    must_exist=True,
                )
            return _resolve(self.default_workdir, path_value, must_exist=True)

        return read_file_explicit(
            path,
            start_line,
            end_line,
            max_file_read_bytes=self.max_file_read_bytes,
            resolve_file=resolve_file,
            display_file=lambda value: _display(
                self.default_workdir,
                value,
                workdir=(binding.workdir if binding is not None else None),
            ),
            snapshot_store=self.store if binding is not None else None,
            snapshot_session_id=(
                binding.session_id if binding is not None else None
            ),
        )


@dataclass(frozen=True)
class _RawSearchMatch:
    match: GrepMatch
    scoped_ranges: LineRanges | None


@dataclass(frozen=True)
class LocalSearchRunner:
    """Local ripgrep process runner with explicit config, path, and grounding dependencies."""

    config: SearchConfig
    """Ripgrep executable and bounded output settings."""
    default_workdir: Path
    """Configured executor default for sessionless path resolution."""
    grounding: SearchGrounding
    """Explicit result grounding and snapshot capability."""

    def _split_scopes(
        self, base: Path, paths: SearchPaths
    ) -> tuple[list[str], list[str], dict[str, LineRanges]]:
        path_args: list[str] = []
        seen_path_args: set[str] = set()
        glob_args: list[str] = []
        line_scopes: dict[str, LineRanges] = {}
        unrestricted_files: set[str] = set()
        for item in search_path_items(paths):
            if looks_like_glob(item):
                glob_args.append(item)
                continue
            path_item, line_ranges = split_line_scoped_search_path(item)
            raw_path = Path(path_item)
            candidate = raw_path if raw_path.is_absolute() else base / raw_path
            resolved = _resolve(
                self.default_workdir, candidate, must_exist=True
            )
            if line_ranges is not None and not resolved.is_file():
                raise ValueError(
                    "search path line selectors are supported only for files"
                )
            if resolved == base:
                path_arg = "."
            elif resolved.is_relative_to(base):
                path_arg = str(resolved.relative_to(base))
            else:
                path_arg = str(resolved)
            if path_arg not in seen_path_args:
                path_args.append(path_arg)
                seen_path_args.add(path_arg)

            resolved_key = str(resolved)
            if line_ranges is None:
                unrestricted_files.add(resolved_key)
                line_scopes.pop(resolved_key, None)
            elif resolved_key not in unrestricted_files:
                line_scopes[resolved_key] = merge_line_ranges(
                    (*line_scopes.get(resolved_key, ()), *line_ranges)
                )
        return path_args, glob_args, line_scopes

    def _ground_match(
        self,
        match: GrepMatch,
        base: Path,
        binding: SessionBinding | None,
    ) -> GrepMatch:
        read_path = search_match_path(str(base), match.path)
        if read_path is None or match.line is None:
            return match
        try:
            read_result = self.grounding.read(
                read_path,
                match.line,
                match.line,
                binding=binding,
            )
        except OSError, UnicodeDecodeError, ValueError:
            return match
        seen_range = (
            read_result.seen_ranges[0] if read_result.seen_ranges else None
        )
        return match.model_copy(
            update={
                "path": read_result.path,
                "numbered_line": read_result.numbered_content,
                "session_id": read_result.session_id,
                "snapshot_id": read_result.snapshot_id,
                "file_sha256": read_result.file_sha256,
                "seen_range": seen_range,
            }
        )

    def _display_output(
        self,
        raw_matches: list[_RawSearchMatch],
        base: Path,
        binding: SessionBinding | None,
        radius: int,
    ) -> tuple[list[GrepDisplayLine], str]:
        displayed: list[GrepDisplayLine] = []
        sections: list[str] = []
        window_matches = [
            SearchWindowMatch(
                path=raw.match.path,
                line=raw.match.line,
                scoped_ranges=raw.scoped_ranges,
            )
            for raw in raw_matches
        ]
        for window in display_windows(window_matches, str(base), radius):
            try:
                read_result = self.grounding.read(
                    window.read_path,
                    window.start,
                    window.end,
                    binding=binding,
                )
            except OSError, UnicodeDecodeError, ValueError:
                continue
            seen_range = (
                read_result.seen_ranges[0] if read_result.seen_ranges else None
            )
            if sections:
                sections.append("")
            sections.append(read_result.numbered_content)
            for line in read_result.lines:
                displayed.append(
                    GrepDisplayLine(
                        path=read_result.path,
                        line=line.line,
                        kind=(
                            "match"
                            if line.line in window.match_lines
                            else "context"
                        ),
                        text=line.text,
                        numbered_line=f"{line.line}:{line.text}",
                        session_id=read_result.session_id,
                        snapshot_id=read_result.snapshot_id,
                        file_sha256=read_result.file_sha256,
                        seen_range=seen_range,
                    )
                )
        return displayed, "\n".join(sections)

    async def search(
        self,
        request: SearchRequest,
        *,
        workdir: str,
        binding: SessionBinding | None = None,
    ) -> GrepSearchOutput:
        """Run one bounded local ripgrep request and ground returned matches."""
        max_results = clamp_search_result_limit(
            request.max_results, self.config.max_results
        )
        skip = max(0, request.skip)
        base = _resolve(self.default_workdir, workdir, must_exist=True)
        path_args, glob_args, line_scopes = self._split_scopes(
            base, request.paths
        )
        args = build_rg_argv(
            self.config,
            request.pattern,
            path_args=path_args,
            glob_args=glob_args,
            regex=request.regex,
            case_sensitive=request.case_sensitive,
            gitignore=request.gitignore,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(base),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return GrepSearchOutput(
                ok=False,
                matches=[],
                displayed_lines=[],
                count=0,
                displayed_count=0,
                context_radius=0,
                skipped=skip,
                truncated=False,
                stderr=f"{self.config.rg_bin}: {exc}",
                numbered_content="",
            )

        matches: list[GrepMatch] = []
        raw_matches: list[_RawSearchMatch] = []
        matched_seen = 0
        stopped_early = False
        timed_out = False
        stderr_bytes = b""

        async def read_stderr() -> bytes:
            if proc.stderr is None:
                return b""
            return await proc.stderr.read(self.config.max_output_bytes + 1)

        stderr_task = asyncio.create_task(read_stderr())
        try:
            if proc.stdout is not None:
                while True:
                    try:
                        line = await asyncio.wait_for(
                            proc.stdout.readline(), timeout=60
                        )
                    except TimeoutError:
                        timed_out = True
                        proc.terminate()
                        break
                    if not line:
                        break
                    parsed = parse_rg_match_record(line)
                    if parsed is None:
                        continue
                    read_path = search_match_path(str(base), parsed.path)
                    if read_path is not None:
                        try:
                            resolved_match_path = str(
                                _resolve(
                                    self.default_workdir,
                                    read_path,
                                    must_exist=True,
                                )
                            )
                        except OSError, ValueError:
                            resolved_match_path = None
                    else:
                        resolved_match_path = None
                    scoped_ranges = (
                        line_scopes.get(resolved_match_path)
                        if resolved_match_path is not None
                        else None
                    )
                    if scoped_ranges is not None and not line_in_ranges(
                        parsed.line, scoped_ranges
                    ):
                        continue
                    if matched_seen < skip:
                        matched_seen += 1
                        continue
                    match = GrepMatch(
                        path=parsed.path,
                        line=parsed.line,
                        column=parsed.column,
                        text=parsed.text,
                    )
                    matches.append(self._ground_match(match, base, binding))
                    raw_matches.append(
                        _RawSearchMatch(
                            match=match,
                            scoped_ranges=scoped_ranges,
                        )
                    )
                    matched_seen += 1
                    if len(matches) >= max_results:
                        stopped_early = True
                        proc.terminate()
                        break
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    proc.wait(), timeout=2 if stopped_early else 60
                )
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
            with suppress(Exception, asyncio.CancelledError):
                stderr_bytes = await stderr_task
            if proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()
                with suppress(Exception, asyncio.CancelledError):
                    await proc.wait()

        stderr_truncated = len(stderr_bytes) > self.config.max_output_bytes
        if stderr_truncated:
            stderr_bytes = stderr_bytes[: self.config.max_output_bytes]
        displayed_lines, numbered_content = self._display_output(
            raw_matches, base, binding, _SEARCH_CONTEXT_RADIUS
        )
        return GrepSearchOutput(
            ok=(not timed_out) and (stopped_early or proc.returncode in {0, 1}),
            matches=matches,
            displayed_lines=displayed_lines,
            count=len(matches),
            displayed_count=len(displayed_lines),
            context_radius=_SEARCH_CONTEXT_RADIUS if matches else 0,
            skipped=skip,
            truncated=stopped_early or stderr_truncated,
            stderr=stderr_bytes.decode(errors="replace"),
            numbered_content=numbered_content,
        )


@dataclass(frozen=True)
class SearchService:
    """Execute each Search operation from a fresh executor session binding."""

    sessions: SessionResolver
    """Authoritative per-operation session binding resolver."""
    local: LocalSearchRunner
    """Ripgrep runner for executor-owned session bindings."""
    max_glob_results: int
    """Executor-owned cap for glob path discovery."""
    max_tree_entries: int
    """Executor-owned cap for tree rendering."""

    async def search(
        self,
        session_id: str,
        pattern: str,
        paths: SearchPaths = None,
        regex: bool = True,
        case_sensitive: bool = True,
        max_results: int | None = None,
        skip: int = 0,
        gitignore: bool = True,
    ) -> GrepSearchOutput:
        """Search one session without caching session or workdir state."""
        request = SearchRequest(
            pattern=pattern,
            paths=paths,
            regex=regex,
            case_sensitive=case_sensitive,
            max_results=max_results,
            skip=skip,
            gitignore=gitignore,
        )
        binding = self.sessions.resolve_active_binding(session_id)
        return await self.local.search(
            request,
            workdir=binding.workdir,
            binding=binding,
        )

    async def glob_search(
        self,
        session_id: str,
        pattern: str,
        cwd: str = ".",
        max_results: int = 500,
    ) -> GlobSearchOutput:
        """Find paths inside one admitted session without ambient policy lookup."""
        binding = self.sessions.resolve_active_binding(session_id)
        base = _resolve(
            self.local.default_workdir,
            cwd,
            workdir=binding.workdir,
            must_exist=True,
        )
        limit = max(1, min(max_results, self.max_glob_results))
        results: list[str] = []
        for item in base.rglob("*"):
            rel = str(item.relative_to(base))
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(
                item.name, pattern
            ):
                results.append(
                    _display(
                        self.local.default_workdir,
                        item,
                        workdir=binding.workdir,
                    )
                )
                if len(results) >= limit:
                    break
        return GlobSearchOutput(paths=results)

    async def tree_view(
        self,
        session_id: str,
        cwd: str = ".",
        depth: int = 3,
        max_entries: int = 500,
    ) -> TreeViewOutput:
        """Render a bounded tree rooted inside one admitted session."""
        binding = self.sessions.resolve_active_binding(session_id)
        base = _resolve(
            self.local.default_workdir,
            cwd,
            workdir=binding.workdir,
            must_exist=False,
        )
        if not base.exists():
            nearest = next(
                (parent for parent in (base, *base.parents) if parent.exists()),
                None,
            )
            entries: list[str] = []
            parent_entries_truncated = False
            if nearest is not None and nearest.is_dir():
                parent_limit = max(0, min(max_entries, 100))
                for child in nearest.iterdir():
                    if child.name == ".git":
                        continue
                    if len(entries) >= parent_limit:
                        parent_entries_truncated = True
                        break
                    entries.append(
                        f"{child.name}/" if child.is_dir() else child.name
                    )
            return TreeViewOutput(
                root=str(base),
                exists=False,
                is_directory=False,
                entries=[],
                count=0,
                truncated=False,
                message=f"Path does not exist: {base}",
                nearest_existing_parent=(
                    str(nearest) if nearest is not None else None
                ),
                nearest_parent_entries=entries,
                nearest_parent_entries_truncated=parent_entries_truncated,
            )
        if not base.is_dir():
            return TreeViewOutput(
                root=str(base),
                exists=True,
                is_directory=False,
                entries=[],
                count=0,
                truncated=False,
                message=f"Path exists but is not a directory: {base}",
            )

        depth = max(0, min(depth, 10))
        limit = max(1, min(max_entries, self.max_tree_entries))
        rows: list[str] = []
        count = 0
        truncated = False

        def walk(directory: Path, current_depth: int) -> None:
            nonlocal count, truncated
            if current_depth >= depth or count >= limit:
                return
            try:
                iterator = directory.iterdir()
            except OSError:
                return
            for path in iterator:
                if path.name == ".git":
                    continue
                if count >= limit:
                    truncated = True
                    return
                rel = path.relative_to(base)
                indent = "  " * (len(rel.parts) - 1)
                suffix = "/" if path.is_dir() else ""
                rows.append(f"{indent}{path.name}{suffix}")
                count += 1
                if path.is_dir():
                    walk(path, current_depth + 1)
                if count >= limit:
                    truncated = True
                    return

        walk(base, 0)
        return TreeViewOutput(
            root=str(base),
            exists=True,
            is_directory=True,
            entries=rows,
            count=count,
            truncated=truncated,
        )
