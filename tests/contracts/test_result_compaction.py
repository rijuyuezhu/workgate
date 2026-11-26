"""Native result compaction preserves the MCP contract and actionable data."""

import copy
import json

import pytest

from workgate.tools.result_compaction import compact_native_result


def _envelope(**updates):
    command = "echo " + "x" * 1000
    data = dict(
        ok=True,
        exit_code=0,
        cwd=".",
        command=command,
        stdout="",
        stderr="",
        timed_out=False,
        truncated=False,
        duration_ms=0,
        extra={"empty": None, "flag": False},
    )
    return {
        "mode": "command",
        "command": command,
        "cwd": ".",
        "result": {**data, **updates},
    }


@pytest.mark.parametrize(
    "name", ["bash", "run_python_code", "browser_run_script"]
)
def test_shell_compaction_preserves_success_and_unknown_fields(name):
    original = _envelope()
    before = copy.deepcopy(original)
    compact = compact_native_result(name, original)
    assert compact == {
        **original,
        "result": {
            "ok": True,
            "exit_code": 0,
            "stdout": "",
            "duration_ms": 0,
            "extra": original["result"]["extra"],
        },
    }
    assert original == before
    assert len(json.dumps(original)) - len(json.dumps(compact)) > 1000
    different = _envelope(command="other", cwd="elsewhere", stderr="warning")
    projected = compact_native_result(name, different)["result"]
    assert (projected["command"], projected["cwd"], projected["stderr"]) == (
        "other",
        "elsewhere",
        "warning",
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"ok": False, "exit_code": 7, "stderr": "failure"},
        {"timed_out": True},
        {"truncated": True},
        {"exit_code": False},
        {"timed_out": 0},
    ],
)
def test_failed_or_incomplete_commands_are_unchanged(updates):
    original = _envelope(**updates)
    assert compact_native_result("bash", original) is original


@pytest.mark.parametrize(
    "name", ["call_agent_mcp_tool", "read", "job", "browser_act"]
)
def test_user_data_is_never_recursively_stripped(name):
    original = _envelope()
    assert compact_native_result(name, original) is original


@pytest.mark.parametrize("mode", ["job", "pty"])
def test_other_shell_modes_are_unchanged(mode):
    original = {**_envelope(), "mode": mode}
    assert compact_native_result("bash", original) is original
