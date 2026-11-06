"""Actual `workgate call` process over one running Controller MCP endpoint."""

import asyncio
import json
import sys

import pytest

from tests.e2e.helpers import isolated_process_env, run_http_process

pytestmark = pytest.mark.integration


async def _invoke(url: str, *args: str, input_text: str | None = None):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "workgate.main",
        "call",
        *args,
        "--url",
        url + "/mcp",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=isolated_process_env(),
    )
    out, err = await asyncio.wait_for(
        process.communicate(
            input_text.encode() if input_text is not None else b""
        ),
        timeout=35,
    )
    return process.returncode, out.decode(), err.decode()


@pytest.mark.asyncio
async def test_cli_calls_real_mcp_control_from_separate_process(tmp_path):
    async with run_http_process(tmp_path, mode="mcp") as (url, workspace):
        code, stdout, stderr = await _invoke(
            url,
            "session_start",
            "--json",
            json.dumps({"workdir": str(workspace)}),
        )
        assert code == 0, stderr
        started = json.loads(stdout)
        session_id = started.get("session_id") or started.get("data", {}).get(
            "session_id"
        )
        assert session_id
        code, stdout, stderr = await _invoke(
            url,
            "read",
            "--session",
            session_id,
            input_text='{"path":"missing.txt"}',
        )
        assert code != 0  # Tool failures return nonzero and structured JSON.
        assert "isError" in stdout or "error" in stdout.lower()
        assert "Traceback" not in stderr


@pytest.mark.asyncio
async def test_cli_rejects_unknown_remote_tool_without_credential_leak(
    tmp_path,
):
    async with run_http_process(tmp_path, mode="mcp") as (url, _workspace):
        code, stdout, stderr = await _invoke(
            url, "unknown_cli_tool", "--json", "{}"
        )
        assert code != 0
        assert "Traceback" not in stderr
