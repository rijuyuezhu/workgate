"""Compact only known native result shapes at the public tool boundary.

Worker RPC, audit records, internal state and arbitrary third-party payloads
retain their original contracts. Missing optional flags have documented defaults.
"""

from typing import Any


def _omit_defaults(
    row: dict[str, Any], defaults: dict[str, Any]
) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if key not in defaults
        or type(value) is not type(defaults[key])
        or value != defaults[key]
    }


def compact_native_result(name: str, result: Any) -> Any:
    """Project only successful bounded shell results, after audit recording.

    The outer resolved command and cwd remain authoritative. Missing nested
    stderr is empty and missing timed_out/truncated flags are false. Keep
    success and exit code explicit for existing callers.
    """
    if (
        name not in {"bash", "run_python_code", "browser_run_script"}
        or not isinstance(result, dict)
        or result.get("mode") != "command"
    ):
        return result
    data = result.get("result")
    if (
        not isinstance(data, dict)
        or data.get("ok") is not True
        or type(data.get("exit_code")) is not int
        or data["exit_code"] != 0
    ):
        return result
    if data.get("timed_out") is not False or data.get("truncated") is not False:
        return result
    compact = _omit_defaults(
        data, {"stderr": "", "timed_out": False, "truncated": False}
    )
    for field in ("command", "cwd"):
        outer = result.get(field)
        if isinstance(outer, str) and compact.get(field) == outer:
            compact.pop(field, None)
    return {**result, "result": compact}
