"""Pure value and decision helpers for content search."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


@dataclass(frozen=True)
class SearchConfig:
    """Narrow ripgrep execution limits required by content Search."""

    rg_bin: str
    """Ripgrep executable used by the local runner."""
    max_results: int
    """Maximum number of actual matches returned by one search."""
    max_output_bytes: int
    """Maximum stderr bytes retained from the ripgrep process."""


type SearchPaths = str | list[str] | None
type LineRanges = tuple[tuple[int, int | None], ...]


@dataclass(frozen=True)
class ParsedSearchMatch:
    """One ripgrep JSON match record projected into typed scalar values."""

    path: str | None
    """Path text emitted by ripgrep, when present."""
    line: int | None
    """One-based matching line number, when present."""
    column: int | None
    """One-based first matching column, when present."""
    text: str
    """Matched source line without its trailing newline."""


@dataclass(frozen=True)
class SearchWindowMatch:
    """One actual match plus optional line-scope bounds for display context."""

    path: str | None
    """Ripgrep match path used to locate the source file."""
    line: int | None
    """One-based line containing the actual match."""
    scoped_ranges: LineRanges | None
    """Optional allowed line ranges inherited from a line-scoped path selector."""


@dataclass(frozen=True)
class SearchDisplayWindow:
    """Merged displayed line window around one or more actual matches."""

    read_path: str
    """File path passed to the grounding reader."""
    start: int
    """First one-based line to display."""
    end: int
    """Last one-based line to display."""
    match_lines: frozenset[int]
    """Actual matching lines inside this display window."""


def clamp_search_result_limit(requested: int | None, configured: int) -> int:
    """Clamp a requested result count to the configured positive maximum."""
    return max(1, min(requested or configured, configured))


def search_match_path(cwd: str, path_text: str | None) -> str | None:
    """Return the file path represented by one ripgrep match path."""
    if path_text is None:
        return None
    path = Path(path_text)
    if path.is_absolute():
        return str(path)
    return str(Path(cwd) / path)


def context_window_for_line(
    line: int, ranges: LineRanges | None, radius: int
) -> tuple[int, int]:
    """Return a context window bounded by an optional allowed line range."""
    start = max(1, line - radius)
    end = line + radius
    if ranges is None:
        return start, end
    for range_start, range_end in ranges:
        if line >= range_start and (range_end is None or line <= range_end):
            start = max(start, range_start)
            if range_end is not None:
                end = min(end, range_end)
            break
    return start, max(start, end)


def display_windows(
    matches: list[SearchWindowMatch], cwd: str, radius: int
) -> list[SearchDisplayWindow]:
    """Build merged per-file display windows around actual matches."""
    windows: list[SearchDisplayWindow] = []
    by_path: dict[str, list[tuple[int, int, int]]] = {}
    for match in matches:
        if match.line is None:
            continue
        read_path = search_match_path(cwd, match.path)
        if read_path is None:
            continue
        start, end = context_window_for_line(
            match.line, match.scoped_ranges, radius
        )
        by_path.setdefault(read_path, []).append((start, end, match.line))

    for read_path, ranges in by_path.items():
        merged: list[SearchDisplayWindow] = []
        for start, end, match_line in sorted(ranges):
            if merged and start <= merged[-1].end + 1:
                previous = merged[-1]
                merged[-1] = SearchDisplayWindow(
                    read_path=previous.read_path,
                    start=previous.start,
                    end=max(previous.end, end),
                    match_lines=previous.match_lines | frozenset({match_line}),
                )
                continue
            merged.append(
                SearchDisplayWindow(
                    read_path=read_path,
                    start=start,
                    end=end,
                    match_lines=frozenset({match_line}),
                )
            )
        windows.extend(merged)
    return windows


_SEARCH_LINE_RANGE_RE = re.compile(r"^(\d+)(?:([-+])(\d*)?)?$")


def parse_search_line_range(part: str) -> tuple[int, int | None] | None:
    """Parse one supported search line selector range."""
    match = _SEARCH_LINE_RANGE_RE.match(part)
    if match is None:
        return None
    start = int(match.group(1))
    mode = match.group(2)
    end_text = match.group(3)
    if start < 1:
        return None
    if mode is None or mode == "-" and not end_text:
        return start, None
    if mode == "-":
        end = int(end_text)
        if end < start:
            return None
        return start, end
    if mode == "+":
        if not end_text:
            return None
        count = int(end_text)
        if count < 1:
            return None
        return start, start + count - 1
    return None


def _looks_like_search_line_selector(selector: str) -> bool:
    return bool(selector) and ("," in selector or selector[0].isdigit())


def split_line_scoped_search_path(path: str) -> tuple[str, LineRanges | None]:
    """Split one path from an optional comma-separated line selector suffix."""
    parts = path.rsplit(":", 1)
    if len(parts) != 2:
        return path, None
    raw_path, raw_ranges = parts
    if not raw_path or not raw_ranges:
        return path, None
    ranges: list[tuple[int, int | None]] = []
    for raw_part in raw_ranges.split(","):
        parsed = parse_search_line_range(raw_part)
        if parsed is None:
            if _looks_like_search_line_selector(raw_ranges):
                raise ValueError(f"invalid search line selector: {raw_ranges}")
            return path, None
        ranges.append(parsed)
    return raw_path, tuple(ranges)


def line_in_ranges(line: int | None, ranges: LineRanges) -> bool:
    """Return whether a line is contained in any inclusive allowed range."""
    if line is None:
        return False
    return any(
        line >= start and (end is None or line <= end) for start, end in ranges
    )


def search_path_items(paths: SearchPaths) -> list[str]:
    """Normalize optional high-level search path scopes into non-empty items."""
    if paths is None:
        return []
    if isinstance(paths, str):
        return [paths] if paths else []
    return [path for path in paths if path]


def looks_like_glob(path: str) -> bool:
    """Return whether a search scope contains glob metacharacters."""
    return any(char in path for char in "*?[")


def merge_line_ranges(ranges: LineRanges) -> LineRanges:
    """Return ordered, coalesced inclusive line ranges."""
    merged: list[tuple[int, int | None]] = []
    for start, end in sorted(
        ranges,
        key=lambda item: (
            item[0],
            float("inf") if item[1] is None else item[1],
        ),
    ):
        if not merged:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        if previous_end is None:
            continue
        if start <= previous_end + 1:
            merged[-1] = (
                previous_start,
                None if end is None else max(previous_end, end),
            )
            continue
        merged.append((start, end))
    return tuple(merged)


def build_rg_argv(
    config: SearchConfig,
    query: str,
    *,
    path_args: list[str],
    glob_args: list[str],
    glob: str | None,
    regex: bool,
    case_sensitive: bool,
    gitignore: bool,
) -> list[str]:
    """Build the ripgrep argv for one already-normalized local search request."""
    args = [config.rg_bin, "--json", "--line-number", "--column"]
    if not gitignore:
        args.append("--no-ignore")
    if not regex:
        args.append("--fixed-strings")
    if not case_sensitive:
        args.append("--ignore-case")
    for glob_arg in glob_args:
        args.extend(["--glob", glob_arg])
    if glob:
        args.extend(["--glob", glob])
    args.extend(["--", query, *(path_args or ["."])])
    return args


def parse_rg_match_record(line: bytes | str) -> ParsedSearchMatch | None:
    """Parse one ripgrep JSON stream line, returning only match records."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError, UnicodeDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    record = cast(dict[str, Any], obj)
    if record.get("type") != "match":
        return None
    data = record.get("data")
    if not isinstance(data, dict):
        return None
    match_data = cast(dict[str, Any], data)
    raw_submatches = match_data.get("submatches")
    submatches = raw_submatches if isinstance(raw_submatches, list) else []
    first = (
        submatches[0] if submatches and isinstance(submatches[0], dict) else {}
    )
    first_start = first.get("start")
    raw_path_data = match_data.get("path")
    raw_line_data = match_data.get("lines")
    path_data = raw_path_data if isinstance(raw_path_data, dict) else {}
    line_data = raw_line_data if isinstance(raw_line_data, dict) else {}
    path_text = path_data.get("text")
    line_number = match_data.get("line_number")
    return ParsedSearchMatch(
        path=path_text if isinstance(path_text, str) else None,
        line=line_number if isinstance(line_number, int) else None,
        column=first_start + 1 if isinstance(first_start, int) else None,
        text=str(line_data.get("text", "")).rstrip("\n"),
    )
