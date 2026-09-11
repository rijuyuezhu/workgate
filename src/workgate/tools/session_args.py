"""Process-neutral extraction of session identifiers from tool arguments."""

from collections.abc import Mapping
from typing import Any


def tool_input_session_ids(value: Any) -> tuple[str, ...]:
    """Extract session ids only from semantic tool-argument positions."""
    found: list[str] = []
    seen: set[int] = set()
    session_names = (
        "session_id",
        "src_session_id",
        "dst_session_id",
        "source_session_id",
        "destination_session_id",
    )
    argument_envelopes = ("kwargs", "keyword_args")

    def visit(candidate: Any) -> None:
        if not isinstance(candidate, Mapping):
            return
        identity = id(candidate)
        if identity in seen:
            return
        seen.add(identity)
        for name in session_names:
            child = candidate.get(name)
            if isinstance(child, str) and child:
                found.append(child)
        for name in argument_envelopes:
            visit(candidate.get(name))

    visit(value)
    return tuple(dict.fromkeys(found))
