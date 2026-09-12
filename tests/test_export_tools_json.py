import json
import os
import subprocess
import sys


def test_export_tools_json_writes_wrapped_payload(tmp_path):
    output = tmp_path / "tools.json"
    env = os.environ.copy()
    env.update(
        {
            "WORKGATE_AGENT_BRIDGE_ENABLED": "false",
            "PYTHONPATH": "src",
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/generation/export-tools-json.py",
            "--wrapped",
            "--output",
            str(output),
        ],
        check=True,
        cwd=".",
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.stdout == ""
    payload = json.loads(output.read_text())
    assert payload["count"] == len(payload["tools"])
    assert {tool["name"] for tool in payload["tools"]} >= {
        "session_start",
        "session_change_cwd",
        "read",
        "bash",
    }


def test_export_tools_json_writes_instruction_markdown_section(tmp_path):
    output = tmp_path / "tools.json"
    instructions_output = tmp_path / "server-instructions.json"
    env = os.environ.copy()
    env.update(
        {
            "WORKGATE_AGENT_BRIDGE_ENABLED": "false",
            "PYTHONPATH": "src",
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/generation/export-tools-json.py",
            "--wrapped",
            "--output",
            str(output),
            "--instructions-output",
            str(instructions_output),
        ],
        check=True,
        cwd=".",
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.stdout == ""
    payload = json.loads(instructions_output.read_text())
    assert payload["sections"][0]["kind"] == "markdown"
    assert "markdown" in payload["sections"][0]
    assert "heading" not in payload["sections"][0]
    assert "code" not in payload["sections"][0]
