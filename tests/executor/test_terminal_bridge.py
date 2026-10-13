import asyncio
import base64
import shutil
import subprocess
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

import workgate.executor.terminal.bridge as bridge_module
from workgate.executor.terminal.bridge import (
    TERMINAL_BRIDGE_BACKEND,
    TerminalBridgeBusyError,
    TerminalBridgeNotFoundError,
    close_terminal_bridge_execute,
    open_terminal_bridge_execute,
    read_terminal_bridge_execute,
    resize_terminal_bridge_execute,
    write_terminal_bridge_execute,
)
from workgate.executor.terminal.runtime import (
    build_terminal_runtime,
    use_terminal_runtime,
)


@pytest.fixture(autouse=True)
async def _terminal_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    runtime = build_terminal_runtime(idle_timeout_s=60)
    await runtime.start()
    try:
        with use_terminal_runtime(runtime):
            yield
    finally:
        await runtime.aclose()


def test_terminal_bridge_policy_is_bounded_at_composition():
    disabled = bridge_module.TerminalBridgeRegistry(
        idle_timeout_s=0, max_connections=0
    )
    oversized = bridge_module.TerminalBridgeRegistry(
        idle_timeout_s=1200, max_connections=999
    )
    short = bridge_module.TerminalBridgeRegistry(
        idle_timeout_s=1, max_connections=7
    )

    assert disabled.idle_timeout_s == 300
    assert disabled.max_connections == 1
    assert oversized.idle_timeout_s == 300
    assert oversized.max_connections == 128
    assert short.idle_timeout_s == 60
    assert short.max_connections == 7


def test_terminal_bridge_retries_short_and_blocked_writes(monkeypatch):
    attach = object.__new__(bridge_module._UnixTmuxAttach)
    attach._closed = False
    attach.master_fd = 7
    attach._write_lock = threading.Lock()
    cast(Any, attach).process = SimpleNamespace(poll=lambda: None)

    attempts = []
    outcomes = [BlockingIOError(), 2, 3]

    def fake_write(fd, data):
        attempts.append((fd, bytes(data)))
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    waits = []

    def fake_select(readable, writable, exceptional, timeout):
        waits.append((readable, writable, exceptional, timeout))
        return [], writable, []

    monkeypatch.setattr(bridge_module.os, "write", fake_write)
    monkeypatch.setattr(bridge_module.select, "select", fake_select)

    attach.write_all(b"abcde")

    assert attempts == [(7, b"abcde"), (7, b"abcde"), (7, b"cde")]
    assert waits == [([], [7], [], 0.1)]
    assert outcomes == []


@pytest.mark.asyncio
async def test_terminal_bridge_registry_bounds_and_lifecycle(monkeypatch):
    instances = []

    class FakeAttach:
        backend = TERMINAL_BRIDGE_BACKEND

        def __init__(self, shell_id, cols, rows):
            self.shell_id = shell_id
            self.cols = cols
            self.rows = rows
            self.closed = False
            self.writes = []
            self.reads = [(b"\x1b[31mready\x1b[0m", False), (b"", False)]
            instances.append(self)

        def read_wait(self, max_bytes, wait_ms):
            assert 1 <= max_bytes <= 65_536
            assert 0 <= wait_ms <= 1_000
            return self.reads.pop(0)

        def write_all(self, data):
            self.writes.append(bytes(data))

        def resize(self, cols, rows):
            self.cols = cols
            self.rows = rows
            return True

        def close_sync(self):
            self.closed = True

    monkeypatch.setattr(bridge_module, "_UnixTmuxAttach", FakeAttach)

    opened = await open_terminal_bridge_execute("demo", 100, 30)
    bridge_id = opened["bridge_id"]
    assert opened == {
        "bridge_id": bridge_id,
        "shell_id": "demo",
        "cols": 100,
        "rows": 30,
        "backend": TERMINAL_BRIDGE_BACKEND,
    }

    second = await open_terminal_bridge_execute("demo", 80, 24)
    second_id = second["bridge_id"]
    assert second_id != bridge_id
    assert (instances[1].cols, instances[1].rows) == (80, 24)

    read = await read_terminal_bridge_execute(bridge_id, 1024, 25)
    assert base64.b64decode(read["data_b64"]) == b"\x1b[31mready\x1b[0m"
    assert read["bytes"] == 14
    assert read["eof"] is False

    payload = b"echo \xff\x00\r"
    written = await write_terminal_bridge_execute(
        bridge_id,
        base64.b64encode(payload).decode("ascii"),
    )
    assert written == {"bridge_id": bridge_id, "written_bytes": len(payload)}
    assert instances[0].writes == [payload]

    resized = await resize_terminal_bridge_execute(bridge_id, 132, 41)
    assert resized == {
        "bridge_id": bridge_id,
        "cols": 132,
        "rows": 41,
        "resized": True,
        "backend": TERMINAL_BRIDGE_BACKEND,
    }
    assert (instances[0].cols, instances[0].rows) == (132, 41)

    assert await close_terminal_bridge_execute(bridge_id) == {
        "bridge_id": bridge_id,
        "closed": True,
    }
    assert instances[0].closed is True
    assert instances[1].closed is False
    assert await close_terminal_bridge_execute(second_id) == {
        "bridge_id": second_id,
        "closed": True,
    }
    assert instances[1].closed is True
    assert await close_terminal_bridge_execute(bridge_id) == {
        "bridge_id": bridge_id,
        "closed": False,
    }
    with pytest.raises(TerminalBridgeNotFoundError):
        await read_terminal_bridge_execute(bridge_id)


@pytest.mark.asyncio
async def test_concurrent_clients_on_same_shell_obey_global_bridge_limit(
    monkeypatch,
):
    registry = bridge_module._bridge_registry()
    registry.max_connections = 2
    entered = asyncio.Event()
    release = asyncio.Event()
    attempts = 0
    attachments = []

    class FakeAttach:
        backend = TERMINAL_BRIDGE_BACKEND

        def __init__(self, shell_id, cols, rows):
            self.closed = False
            attachments.append(self)

        def close_sync(self):
            self.closed = True

    real_to_thread = asyncio.to_thread

    async def wait_for_attach(function, *args, **kwargs):
        nonlocal attempts
        if function is FakeAttach:
            attempts += 1
            if attempts == 2:
                entered.set()
            await release.wait()
            return FakeAttach(*args)
        return await real_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(bridge_module, "_UnixTmuxAttach", FakeAttach)
    monkeypatch.setattr(bridge_module.asyncio, "to_thread", wait_for_attach)
    first = asyncio.create_task(open_terminal_bridge_execute("shared-shell"))
    second = asyncio.create_task(open_terminal_bridge_execute("shared-shell"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert len(registry.pending_bridges) == 2
        with pytest.raises(TerminalBridgeBusyError, match="Too many active"):
            await open_terminal_bridge_execute("shared-shell")
    finally:
        release.set()
    first_bridge, second_bridge = await asyncio.gather(first, second)
    assert first_bridge["bridge_id"] != second_bridge["bridge_id"]
    assert len(registry.bridges) == 2
    assert not registry.pending_bridges
    await close_terminal_bridge_execute(first_bridge["bridge_id"])
    assert len(registry.bridges) == 1
    assert len([attach for attach in attachments if attach.closed]) == 1
    await close_terminal_bridge_execute(second_bridge["bridge_id"])
    assert all(attach.closed for attach in attachments)


@pytest.mark.asyncio
async def test_terminal_bridge_rejects_malformed_capabilities_and_chunks(
    monkeypatch,
):
    class FakeAttach:
        backend = TERMINAL_BRIDGE_BACKEND

        def __init__(self, shell_id, cols, rows):
            self.closed = False

        def close_sync(self):
            self.closed = True

    monkeypatch.setattr(bridge_module, "_UnixTmuxAttach", FakeAttach)
    opened = await open_terminal_bridge_execute("demo", 80, 24)

    with pytest.raises(TerminalBridgeNotFoundError):
        await read_terminal_bridge_execute("../bad")
    with pytest.raises(ValueError, match="valid base64"):
        await write_terminal_bridge_execute(opened["bridge_id"], "not base64!")
    with pytest.raises(ValueError, match="too large"):
        await write_terminal_bridge_execute(
            opened["bridge_id"],
            base64.b64encode(b"x" * 65_537).decode("ascii"),
        )
    with pytest.raises(ValueError, match="cols must be between"):
        await resize_terminal_bridge_execute(opened["bridge_id"], 2, 24)


@pytest.mark.skipif(
    shutil.which("tmux") is None or shutil.which("bash") is None,
    reason="requires POSIX tmux and bash",
)
@pytest.mark.asyncio
async def test_real_terminal_bridge_streams_raw_bytes_and_preserves_tmux_session(
    monkeypatch,
    tmp_path,
):
    shell_id = f"workgate-bridge-{uuid.uuid4().hex[:10]}"
    tmux = shutil.which("tmux") or "tmux"
    bash = shutil.which("bash") or "bash"
    ready_marker = b"BRIDGE_READY> "
    bashrc = tmp_path / "bridge-test.bashrc"
    bashrc.write_text(
        "unset PROMPT_COMMAND\nPS1='BRIDGE_READY> '\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            tmux,
            "new-session",
            "-d",
            "-s",
            shell_id,
            bash,
            "--noprofile",
            "--rcfile",
            str(bashrc),
            "-i",
        ],
        check=True,
    )

    async def read_until(marker: bytes, attachment_id: str) -> bytes:
        deadline = time.monotonic() + 10
        output = bytearray()
        while time.monotonic() < deadline and marker not in output:
            chunk = await read_terminal_bridge_execute(
                attachment_id, 65_536, 100
            )
            output.extend(base64.b64decode(chunk["data_b64"]))
            if chunk["eof"]:
                break
        assert marker in output, bytes(output[-4096:])
        return bytes(output)

    bridge_id = ""
    second_id = ""
    try:
        opened = await open_terminal_bridge_execute(shell_id, 90, 28)
        bridge_id = opened["bridge_id"]
        # The raw bridge normally connects to xterm.js, which answers tmux's
        # bounded capability, color, and size queries. Reproduce that handshake,
        # then clear any unrecognized queued input and submit an empty line. The
        # fresh Bash prompt proves attach initialization can accept application
        # input before the real assertion command is written.
        terminal_responses = (
            b"\x1b[?62;4;9;22c"
            b"\x1b[>0;276;0c"
            b"\x1b]10;rgb:d8d8/e9e9/f5f5\x1b\\"
            b"\x1b]11;rgb:0707/1111/1f1f\x1b\\"
            b"\x1b[8;28;90t"
            b"\x1b[4;532;738t"
            b"\x15\r"
        )
        await write_terminal_bridge_execute(
            bridge_id,
            base64.b64encode(terminal_responses).decode("ascii"),
        )
        await read_until(ready_marker, bridge_id)

        command = b"printf '\\033[31mBRIDGE_RAW_OK\\033[0m\\n'\r"
        await write_terminal_bridge_execute(
            bridge_id,
            base64.b64encode(command).decode("ascii"),
        )
        await read_until(b"\x1b[31mBRIDGE_RAW_OK", bridge_id)

        # tmux supports multiple independently attached clients per session.
        second = await open_terminal_bridge_execute(shell_id, 80, 24)
        second_id = second["bridge_id"]
        assert second_id != bridge_id
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            clients = subprocess.run(
                [tmux, "list-clients", "-t", f"={shell_id}"],
                check=True,
                capture_output=True,
            )
            if len(clients.stdout.splitlines()) >= 2:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("tmux did not attach two clients")

        # Closing one WebUI's PTY cannot close the other or the tmux session.
        await resize_terminal_bridge_execute(bridge_id, 120, 35)
        await close_terminal_bridge_execute(bridge_id)
        bridge_id = ""
        await write_terminal_bridge_execute(
            second_id,
            base64.b64encode(terminal_responses).decode("ascii"),
        )
        await read_until(ready_marker, second_id)
        await write_terminal_bridge_execute(
            second_id,
            base64.b64encode(b"printf 'MULTI_CLIENT_OK\\n'\r").decode("ascii"),
        )
        await read_until(b"MULTI_CLIENT_OK", second_id)
        await close_terminal_bridge_execute(second_id)
        second_id = ""
        assert (
            subprocess.run(
                [tmux, "has-session", "-t", shell_id],
                check=False,
                capture_output=True,
            ).returncode
            == 0
        )
    finally:
        if bridge_id:
            await close_terminal_bridge_execute(bridge_id)
        if second_id:
            await close_terminal_bridge_execute(second_id)
        subprocess.run(
            [tmux, "kill-session", "-t", shell_id],
            check=False,
            capture_output=True,
        )


def test_unix_terminal_bridge_uses_resolved_tmux(monkeypatch):
    calls = []
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.setattr(
        bridge_module,
        "require_tmux",
        lambda: SimpleNamespace(path="/bundle/tmux"),
    )

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=1, stderr=b"missing", stdout=b"")

    monkeypatch.setattr(bridge_module.subprocess, "run", fake_run)
    with pytest.raises(TerminalBridgeNotFoundError, match="missing"):
        bridge_module._UnixTmuxAttach("demo", 80, 24)
    assert calls[0][0] == ["/bundle/tmux", "has-session", "-t", "=demo"]
    assert "TMUX" not in calls[0][1]["env"]
    assert "TMUX_PANE" not in calls[0][1]["env"]


@pytest.mark.asyncio
async def test_terminal_bridge_eof_releases_attachment_without_killing_shell(
    monkeypatch,
):
    attachments = []

    class EofAttach:
        backend = TERMINAL_BRIDGE_BACKEND

        def __init__(self, shell_id, cols, rows):
            self.shell_id = shell_id
            self.closed = False
            attachments.append(self)

        def read_wait(self, max_bytes, wait_ms):
            return b"", True

        def close_sync(self):
            self.closed = True

    monkeypatch.setattr(bridge_module, "_UnixTmuxAttach", EofAttach)
    opened = await open_terminal_bridge_execute("eof-demo")
    result = await read_terminal_bridge_execute(opened["bridge_id"])
    assert result["eof"] is True
    assert result["bytes"] == 0
    assert attachments[0].closed
    with pytest.raises(TerminalBridgeNotFoundError):
        await read_terminal_bridge_execute(opened["bridge_id"])


@pytest.mark.asyncio
async def test_terminal_bridge_expiry_reschedules_live_attachment_and_closes_idle_one(
    monkeypatch,
):
    registry = bridge_module._bridge_registry()
    closed = []
    process = SimpleNamespace(close_sync=lambda: closed.append("closed"))
    bridge = bridge_module._Bridge(
        bridge_id="A" * 32,
        shell_id="idle-shell",
        process=cast(Any, process),
        cols=80,
        rows=24,
        idle_timeout_s=60,
    )
    registry.bridges[bridge.bridge_id] = bridge
    scheduled = []
    monkeypatch.setattr(
        bridge_module,
        "_schedule_expiry_locked",
        lambda _registry, _bridge, delay=None: scheduled.append(delay),
    )
    bridge_module._expire_bridge(registry, bridge.bridge_id)
    assert len(scheduled) == 1
    assert scheduled[0] > 0
    assert bridge.bridge_id in registry.bridges

    bridge.last_activity -= 120
    bridge_module._expire_bridge(registry, bridge.bridge_id)
    assert closed == ["closed"]
    assert bridge.bridge_id not in registry.bridges
    bridge_module._expire_bridge(registry, bridge.bridge_id)  # idempotent


@pytest.mark.asyncio
async def test_terminal_bridge_registry_propagates_posix_attachment_cleanup_failure():
    registry = bridge_module.TerminalBridgeRegistry()
    await registry.start()

    def failed_close():
        raise OSError("PTY close failure")

    bridge = bridge_module._Bridge(
        bridge_id="B" * 32,
        shell_id="error-shell",
        process=cast(Any, SimpleNamespace(close_sync=failed_close)),
        cols=80,
        rows=24,
        idle_timeout_s=60,
    )
    registry.bridges[bridge.bridge_id] = bridge
    with pytest.raises(RuntimeError, match="failed to close 1 terminal bridge"):
        await registry.aclose()
    assert not registry.bridges


@pytest.mark.asyncio
async def test_terminal_bridge_open_cancellation_releases_created_posix_attachment(
    monkeypatch,
):
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()
    attached = []

    class SlowAttach:
        backend = TERMINAL_BRIDGE_BACKEND

        def __init__(self, shell_id, cols, rows):
            self.closed = False
            attached.append(self)

        def close_sync(self):
            self.closed = True

    real_to_thread = asyncio.to_thread

    async def wait_for_attach(function, *args, **kwargs):
        if function is SlowAttach:
            started.set()
            await release.wait()
            return SlowAttach(*args)
        return await real_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(bridge_module, "_UnixTmuxAttach", SlowAttach)
    monkeypatch.setattr(bridge_module.asyncio, "to_thread", wait_for_attach)
    opening = asyncio.create_task(open_terminal_bridge_execute("cancel-demo"))
    await asyncio.wait_for(started.wait(), 1)
    opening.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(opening, 2)
    assert len(attached) == 1
    assert attached[0].closed
    assert not bridge_module._bridge_registry().pending_bridges
