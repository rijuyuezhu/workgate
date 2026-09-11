"""Parse path selectors for agent read tools."""

import re
from dataclasses import dataclass

_LINE_SELECTOR_RE = re.compile(
    r"^(?P<start>\d+)(?:(?P<mode>[-+])(?P<end>\d*)?)?$"
)


type LineRangeSelector = tuple[int, int | None]


@dataclass(frozen=True)
class ReadTarget:
    """A path plus optional display selectors parsed from one read target."""

    path: str
    """Workspace path with recognized selector suffixes removed."""

    start_line: int | None
    """Optional 1-based first line selected by the target."""

    end_line: int | None
    """Optional 1-based final line selected by the target."""

    line_ranges: tuple[LineRangeSelector, ...]
    """Line ranges selected by the target, in display order."""

    raw: bool
    """Whether model-facing output should omit added line-number prefixes."""


def _parse_line_selector(selector: str) -> LineRangeSelector | None:
    """Return a 1-based line range for a supported line selector."""
    match = _LINE_SELECTOR_RE.match(selector)
    if match is None:
        return None
    start = int(match.group("start"))
    mode = match.group("mode")
    end_text = match.group("end")
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


def _looks_like_line_selector(selector: str) -> bool:
    """Return whether a malformed suffix appears to be an intended line selector."""
    return bool(selector) and ("," in selector or selector[0].isdigit())


def _parse_line_ranges_selector(
    selector: str,
) -> tuple[LineRangeSelector, ...] | None:
    """Parse one line selector suffix into validated display ranges."""
    if "," not in selector:
        parsed = _parse_line_selector(selector)
        if parsed is not None:
            return (parsed,)
        if _looks_like_line_selector(selector):
            raise ValueError(f"invalid read line selector: {selector}")
        return None

    ranges: list[LineRangeSelector] = []
    previous_end: int | None = None
    pieces = selector.split(",")
    for index, piece in enumerate(pieces):
        parsed = _parse_line_selector(piece)
        if parsed is None:
            raise ValueError(f"invalid read line selector: {selector}")
        start, end = parsed
        if end is None and index != len(pieces) - 1:
            raise ValueError(
                "open-ended read range is only allowed as the final range"
            )
        if previous_end is not None and start <= previous_end:
            raise ValueError(
                "read line ranges must be ordered and non-overlapping"
            )
        ranges.append(parsed)
        previous_end = end
    return tuple(ranges)


def parse_read_target(target: str) -> ReadTarget:
    """Parse a read target with optional ':raw' and line-range suffixes.

    ':50' and ':50-' read from line 50 through the end, ':50-80' reads an
    inclusive range, ':50+20' reads 20 lines starting at line 50, and ':raw'
    keeps the model-facing output unnumbered. Comma-separated ranges such as
    ':5-16,960-973' request multiple non-overlapping windows from one file.
    Suffixes may be combined as ':50-80:raw' or ':raw:50-80'.
    """
    if not target:
        raise ValueError("read target path must not be empty")

    parts = target.split(":")
    raw = False
    line_ranges: tuple[LineRangeSelector, ...] = ()
    while len(parts) > 1:
        suffix = parts[-1]
        if suffix == "raw":
            raw = True
            parts.pop()
            continue
        parsed_ranges = _parse_line_ranges_selector(suffix)
        if parsed_ranges is not None and not line_ranges:
            line_ranges = parsed_ranges
            parts.pop()
            continue
        break

    path = ":".join(parts)
    if not path:
        raise ValueError("read target path must not be empty")
    start_line = line_ranges[0][0] if line_ranges else None
    end_line = line_ranges[-1][1] if line_ranges else None
    return ReadTarget(
        path=path,
        start_line=start_line,
        end_line=end_line,
        line_ranges=line_ranges,
        raw=raw,
    )
