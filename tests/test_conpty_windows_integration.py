import asyncio
import os
import time
import uuid

import pytest

import workgate.executor.terminal.conpty as conpty
from workgate.config.settings import clear_settings_cache
from workgate.executor.terminal.runtime import build_terminal_runtime
from workgate.persistence import get_state_store

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not conpty.is_available(),
    reason="requires Windows ConPTY and pywinpty",
)


async def _wait_for_snapshot(shell_id: str, marker: str) -> str:
    deadline = time.monotonic() + 10
    output = ""
    while time.monotonic() < deadline:
        result = await conpty.read_shell(
            shell_id,
            200,
            preserve_ansi=False,
        )
        output = result.output
        if marker in output:
            return output
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"ConPTY snapshot did not contain {marker!r}: {output[-2000:]}"
    )


async def _wait_for_raw(
    attachment: conpty.ConPtyRawAttachment,
    marker: bytes,
) -> bytes:
    deadline = time.monotonic() + 10
    output = bytearray()
    while time.monotonic() < deadline:
        data, eof = await asyncio.to_thread(
            attachment.read_wait,
            conpty.CONPTY_READ_CHARS,
            250,
        )
        output.extend(data)
        if marker in output:
            return bytes(output)
        if eof:
            break
    raise AssertionError(
        f"ConPTY raw stream did not contain {marker!r}: {bytes(output[-2000:])!r}"
    )


@pytest.mark.asyncio
async def test_real_windows_conpty_persistent_shell_and_raw_bridge(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    runtime = build_terminal_runtime(get_state_store(), workspace_root=tmp_path)
    await runtime.start()

    shell_id = f"conpty-{uuid.uuid4().hex[:12]}"
    snapshot_marker = f"SNAPSHOT_{uuid.uuid4().hex}"
    raw_marker = f"RAW_{uuid.uuid4().hex}"
    attachment: conpty.ConPtyRawAttachment | None = None

    try:
        started = await conpty.start_shell(
            shell_id=shell_id,
            cwd=tmp_path,
            command=None,
        )
        assert started.backend == "conpty"
        assert started.shell_id == shell_id

        await conpty.send_shell(
            shell_id,
            f"echo {snapshot_marker}",
            True,
        )
        snapshot = await _wait_for_snapshot(shell_id, snapshot_marker)
        assert snapshot_marker in snapshot

        attachment = conpty.open_raw_attachment(
            shell_id,
            f"attachment-{uuid.uuid4().hex}",
            100,
            30,
        )
        attachment.write_all(f"echo {raw_marker}\r".encode())
        raw = await _wait_for_raw(attachment, raw_marker.encode())
        assert raw_marker.encode() in raw

        resized = attachment.resize(120, 40)
        assert isinstance(resized, bool)

        attachment.close_sync()
        attachment = None
        listed = await conpty.list_shells()
        assert shell_id in {item.shell_id for item in listed.shells}
    finally:
        if attachment is not None:
            attachment.close_sync()
        if shell_id in {
            item.shell_id for item in (await conpty.list_shells()).shells
        }:
            await conpty.kill_shell(shell_id)
        await runtime.aclose()
        clear_settings_cache()
