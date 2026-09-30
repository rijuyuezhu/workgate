"""Explicit model-facing text projections for structured MCP tool results."""

import json
from collections.abc import Callable, Mapping
from typing import Any

ToolTextRenderer = Callable[[Mapping[str, Any]], str]


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError
    return value


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError
    return value


def _rows(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(
        isinstance(row, Mapping) for row in value
    ):
        raise TypeError
    return list(value)


def _optional_string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _read_text(result: Mapping[str, Any]) -> str:
    return _string(result["content"])


def _search_text(result: Mapping[str, Any]) -> str:
    numbered = _string(result["numbered_content"])
    if numbered:
        return numbered
    stderr = _optional_string(result.get("stderr")).strip()
    if stderr:
        return stderr
    if result.get("count") == 0:
        return "No matches."
    raise TypeError


def _tree_text(result: Mapping[str, Any]) -> str:
    entries = [str(entry) for entry in _rows_or_strings(result["entries"])]
    parts: list[str] = []
    message = _optional_string(result.get("message"))
    if message:
        parts.append(message)
    if entries:
        parts.append("\n".join(entries))
        if result.get("truncated") is True:
            parts.append("[tree truncated]")
    else:
        parent = _optional_string(result.get("nearest_existing_parent"))
        parent_entries = result.get("nearest_parent_entries")
        if parent and isinstance(parent_entries, list):
            parts.append(f"Nearest existing parent: {parent}")
            if parent_entries:
                parts.append("\n".join(str(entry) for entry in parent_entries))
            if result.get("nearest_parent_entries_truncated") is True:
                parts.append("[nearest parent listing truncated]")
    return "\n".join(parts) or "(empty tree)"


def _glob_text(result: Mapping[str, Any]) -> str:
    paths = [str(path) for path in _rows_or_strings(result["paths"])]
    return "\n".join(paths) if paths else "No matches."


def _rows_or_strings(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError
    return value


def _edit_lines_text(result: Mapping[str, Any]) -> str:
    return _string(_mapping(result["context"])["numbered_content"])


def _hashline_edit_text(result: Mapping[str, Any]) -> str:
    return "\n\n".join(
        _string(_mapping(hunk["context"])["numbered_content"])
        for hunk in _rows(result["hunks"])
    )


def _command_text(result: Mapping[str, Any]) -> str:
    parts: list[str] = []
    stdout = _string(result["stdout"]).rstrip("\n")
    stderr = _string(result["stderr"]).rstrip("\n")
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    if result.get("timed_out") is True:
        parts.append("[timed out]")
    else:
        exit_code = result.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            parts.append(f"[exit code {exit_code}]")
    if result.get("truncated") is True:
        parts.append("[output truncated]")
    return "\n".join(parts) or "Command completed with no output."


def _shell_text(result: Mapping[str, Any]) -> str:
    detail = _mapping(result["result"])
    match result["mode"]:
        case "command":
            return _command_text(detail)
        case "job":
            return f"Job {_string(detail['job_id'])} ({_string(detail['status'])})."
        case "pty":
            return f"Persistent shell {_string(detail['shell_id'])} started."
        case _:
            raise TypeError


def _persistent_shell_text(result: Mapping[str, Any]) -> str:
    return _string(result["output"]) or "No terminal output."


def _job_text(result: Mapping[str, Any]) -> str:
    match result["operation"]:
        case "poll":
            chunks = []
            for row in _rows(result["outputs"]):
                job = _mapping(row["job"])
                details: list[str] = []
                output = _string(row["output"]).rstrip("\n")
                if output:
                    details.append(output)
                message = _optional_string(row.get("message"))
                if message:
                    details.append(message)
                progress = job.get("progress")
                job_result = job.get("result")
                if isinstance(job_result, Mapping):
                    details.append(
                        "[result]\n"
                        + json.dumps(
                            job_result, ensure_ascii=False, sort_keys=True
                        )
                    )
                elif isinstance(progress, Mapping):
                    details.append(
                        "[progress]\n"
                        + json.dumps(
                            progress, ensure_ascii=False, sort_keys=True
                        )
                    )
                error = _optional_string(job.get("error")).strip()
                if error:
                    details.append(f"[error]\n{error}")
                if not details:
                    details.append("No output.")
                chunks.append(
                    f"[{_string(job['job_id'])} {_string(job['status'])}]\n"
                    + "\n".join(details)
                )
            return "\n\n".join(chunks) or "No job output."
        case "list":
            jobs = _rows(result["jobs"])
            message = _optional_string(result.get("message"))
            if not jobs:
                return message or "No tracked jobs."
            rows = "\n".join(
                "\t".join(
                    part
                    for part in (
                        _string(job["job_id"]),
                        _string(job["status"]),
                        _optional_string(job.get("name")),
                    )
                    if part
                )
                for job in jobs
            )
            return f"{message}\n{rows}" if message else rows
        case "cancel":
            lines = []
            for row in _rows(result["cancelled"]):
                job = _mapping(row["job"])
                state = (
                    "stopped" if row.get("killed") is True else "not stopped"
                )
                stderr = _optional_string(row.get("stderr")).strip()
                lines.append(
                    f"{_string(job['job_id'])}: {state}"
                    + (f"\n[stderr]\n{stderr}" if stderr else "")
                )
            return "\n\n".join(lines) or "No jobs cancelled."
        case "retry":
            rows = _rows(result["retried"])
            return (
                "\n".join(
                    f"{_string(row['job_id'])}: {_string(row['status'])}"
                    for row in rows
                )
                or "No jobs retried."
            )
        case _:
            raise TypeError


def _skill_text(result: Mapping[str, Any]) -> str:
    content = _string(result["content"])
    related = _rows_or_strings(result["related_files"])
    if not related:
        return content
    separator = "" if content.endswith("\n") else "\n"
    return f"{content}{separator}\n[related files]\n" + "\n".join(
        str(path) for path in related
    )


def _content_text(result: Mapping[str, Any]) -> str:
    return _string(result["content"])


def _fetch_text(result: Mapping[str, Any]) -> str:
    return _string(result["text"])


def _session_text(result: Mapping[str, Any]) -> str:
    first = f"Session {_string(result['session_id'])} ready in {_string(result['workdir'])}."
    message = _optional_string(result.get("message"))
    return f"{first}\n{message}" if message else first


_RENDERERS: dict[str, ToolTextRenderer] = {
    "read": _read_text,
    "search": _search_text,
    "tree_view": _tree_text,
    "glob_search": _glob_text,
    "edit_lines": _edit_lines_text,
    "hashline_edit": _hashline_edit_text,
    "bash": _shell_text,
    "run_python_code": _shell_text,
    "read_persistent_shell_output": _persistent_shell_text,
    "job": _job_text,
    "activate_agent_skill": _skill_text,
    "read_agent_skill_file": _content_text,
    "fetch": _fetch_text,
    "session_start": _session_text,
    "session_change_cwd": _session_text,
}


def render_tool_text(name: str, structured: Any) -> str | None:
    """Return explicit model-facing text for one tool, or None for JSON fallback."""
    renderer = _RENDERERS.get(name)
    if renderer is None or not isinstance(structured, Mapping):
        return None
    try:
        return renderer(structured)
    except KeyError, TypeError:
        return None


def has_explicit_tool_text(name: str) -> bool:
    """Return whether a tool has an explicit model-facing text projection."""
    return name in _RENDERERS
