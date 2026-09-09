import time
from pathlib import Path
from typing import Any, cast

import pytest

import workgate.executor.terminal.bridge as bridge_module
import workgate.executor.terminal.conpty as conpty_module
import workgate.executor.terminal.runtime as terminal_runtime_module
from workgate.executor.terminal.bridge import TerminalBridgeError, _Bridge
from workgate.executor.terminal.conpty import _ConPtySession
from workgate.executor.terminal.runtime import (
    build_terminal_runtime,
)
from workgate.persistence import get_state_store


class _BridgeProcess:
    backend = "test"
    shell_id = "shell"

    def __init__(self) -> None:
        self.closed = False

    def read_wait(self, max_bytes: int, wait_ms: int) -> tuple[bytes, bool]:
        return b"", False

    def write_all(self, data: bytes) -> None:
        return None

    def resize(self, cols: int, rows: int) -> bool:
        return True

    def close_sync(self) -> None:
        self.closed = True


class _FailingBridgeProcess(_BridgeProcess):
    def close_sync(self) -> None:
        self.closed = True
        raise OSError("bridge close failed")


class _ConPtyProcess:
    def __init__(self, *, fail_first_close: bool = False) -> None:
        self.alive = True
        self.fail_first_close = fail_first_close
        self.close_calls = 0

    def isalive(self) -> bool:
        return self.alive

    def close(self, force: bool = False) -> None:
        self.close_calls += 1
        if self.fail_first_close and self.close_calls == 1:
            raise OSError("close failed")
        self.alive = False


class _Lease:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


@pytest.mark.asyncio
async def test_terminal_runtime_installs_and_restores_nested_bindings() -> None:
    outer = build_terminal_runtime(get_state_store(), workspace_root=Path.cwd())
    inner = build_terminal_runtime(get_state_store(), workspace_root=Path.cwd())

    await outer.start()
    try:
        assert bridge_module._bridge_registry() is outer.bridges
        assert conpty_module._conpty_registry() is outer.conpty

        await inner.start()
        try:
            assert bridge_module._bridge_registry() is inner.bridges
            assert conpty_module._conpty_registry() is inner.conpty
        finally:
            await inner.aclose()

        assert bridge_module._bridge_registry() is outer.bridges
        assert conpty_module._conpty_registry() is outer.conpty
        await inner.aclose()
    finally:
        await outer.aclose()

    with pytest.raises(RuntimeError, match="not configured"):
        bridge_module._bridge_registry()
    with pytest.raises(RuntimeError, match="not configured"):
        conpty_module._conpty_registry()


@pytest.mark.asyncio
async def test_terminal_runtime_start_is_idempotent_and_close_is_terminal() -> (
    None
):
    runtime = build_terminal_runtime(
        get_state_store(), workspace_root=Path.cwd()
    )

    await runtime.start()
    await runtime.start()
    await runtime.aclose()

    with pytest.raises(RuntimeError, match="cannot be restarted"):
        await runtime.start()


@pytest.mark.asyncio
async def test_terminal_runtime_lifespan_owns_and_releases_bindings() -> None:
    runtime = build_terminal_runtime(
        get_state_store(), workspace_root=Path.cwd()
    )

    async with runtime.lifespan() as active:
        assert active is runtime
        assert bridge_module._bridge_registry() is runtime.bridges
        assert conpty_module._conpty_registry() is runtime.conpty

    with pytest.raises(RuntimeError, match="not configured"):
        bridge_module._bridge_registry()
    with pytest.raises(RuntimeError, match="not configured"):
        conpty_module._conpty_registry()


@pytest.mark.asyncio
async def test_terminal_runtime_rolls_back_when_bridge_binding_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_terminal_runtime(
        get_state_store(), workspace_root=Path.cwd()
    )

    def fail_bridge_binding(_registry: object) -> object:
        raise RuntimeError("bridge binding failed")

    monkeypatch.setattr(
        terminal_runtime_module,
        "configure_terminal_bridge_registry",
        fail_bridge_binding,
    )

    with pytest.raises(RuntimeError, match="bridge binding failed"):
        await runtime.start()

    assert runtime.bridges._closed is True
    assert runtime.conpty._closed is True
    with pytest.raises(RuntimeError, match="not configured"):
        conpty_module._conpty_registry()


@pytest.mark.asyncio
async def test_terminal_runtime_stops_admission_before_dependency_ordered_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_terminal_runtime(
        get_state_store(), workspace_root=Path.cwd()
    )
    await runtime.start()
    events: list[str] = []

    bridge_stop = runtime.bridges.stop_admission
    conpty_stop = runtime.conpty.stop_admission
    bridge_close = runtime.bridges.aclose
    conpty_close = runtime.conpty.aclose

    def stop_bridges() -> None:
        events.append("bridge-stop")
        bridge_stop()

    def stop_conpty() -> None:
        events.append("conpty-stop")
        conpty_stop()

    async def close_bridges() -> None:
        events.append("bridge-close")
        await bridge_close()

    async def close_conpty() -> None:
        events.append("conpty-close")
        await conpty_close()

    monkeypatch.setattr(runtime.bridges, "stop_admission", stop_bridges)
    monkeypatch.setattr(runtime.conpty, "stop_admission", stop_conpty)
    monkeypatch.setattr(runtime.bridges, "aclose", close_bridges)
    monkeypatch.setattr(runtime.conpty, "aclose", close_conpty)

    await runtime.aclose()

    assert events[:4] == [
        "bridge-stop",
        "conpty-stop",
        "bridge-close",
        "bridge-stop",
    ]
    assert events.index("bridge-close") < events.index("conpty-close")
    with pytest.raises(TerminalBridgeError, match="closed"):
        runtime.bridges.require_open()
    with pytest.raises(RuntimeError, match="closed"):
        runtime.conpty.require_open()


@pytest.mark.asyncio
async def test_terminal_runtime_closes_registered_bridge_and_conpty_resources() -> (
    None
):
    runtime = build_terminal_runtime(
        get_state_store(), workspace_root=Path.cwd()
    )
    await runtime.start()
    bridge_process = _BridgeProcess()
    conpty_process = _ConPtyProcess()
    lease = _Lease()

    bridge = _Bridge(
        bridge_id="bridge",
        shell_id="shell",
        process=bridge_process,
        cols=80,
        rows=24,
        idle_timeout_s=60,
    )
    with runtime.bridges.lock:
        runtime.bridges.bridges[bridge.bridge_id] = bridge
        runtime.bridges.shell_bridges[bridge.shell_id] = bridge.bridge_id

    conpty_session = _ConPtySession(
        shell_id="conpty",
        process=conpty_process,
        lease=cast(Any, lease),
        registry=runtime.conpty,
        cwd=Path("."),
        command="cmd.exe",
        created=1,
    )
    with runtime.conpty.lock:
        runtime.conpty.sessions[conpty_session.shell_id] = conpty_session

    await runtime.aclose()

    assert bridge_process.closed is True
    assert runtime.bridges.bridges == {}
    assert runtime.bridges.shell_bridges == {}
    assert conpty_process.alive is False
    assert lease.released is True
    assert runtime.conpty.sessions == {}


@pytest.mark.asyncio
async def test_terminal_runtime_reports_bridge_close_failure_after_conpty_close() -> (
    None
):
    runtime = build_terminal_runtime(
        get_state_store(), workspace_root=Path.cwd()
    )
    await runtime.start()
    bridge_process = _FailingBridgeProcess()
    bridge = _Bridge(
        bridge_id="broken-bridge",
        shell_id="shell",
        process=bridge_process,
        cols=80,
        rows=24,
        idle_timeout_s=60,
    )
    with runtime.bridges.lock:
        runtime.bridges.bridges[bridge.bridge_id] = bridge
        runtime.bridges.shell_bridges[bridge.shell_id] = bridge.bridge_id

    with pytest.raises(RuntimeError, match="failed to close 1 terminal bridge"):
        await runtime.aclose()

    assert bridge_process.closed is True
    assert runtime.bridges.bridges == {}
    assert runtime.conpty._closed is True
    with pytest.raises(RuntimeError, match="not configured"):
        bridge_module._bridge_registry()


@pytest.mark.asyncio
async def test_terminal_bridge_registry_expires_owned_bridge() -> None:
    runtime = build_terminal_runtime(
        get_state_store(), workspace_root=Path.cwd()
    )
    await runtime.start()
    process = _BridgeProcess()
    bridge = _Bridge(
        bridge_id="expired-bridge",
        shell_id="shell",
        process=process,
        cols=80,
        rows=24,
        idle_timeout_s=1,
        last_activity=time.monotonic() - 2,
    )
    with runtime.bridges.lock:
        runtime.bridges.bridges[bridge.bridge_id] = bridge
        runtime.bridges.shell_bridges[bridge.shell_id] = bridge.bridge_id

    bridge_module._expire_bridge(runtime.bridges, bridge.bridge_id)

    assert process.closed is True
    assert runtime.bridges.bridges == {}
    assert runtime.bridges.shell_bridges == {}
    await runtime.aclose()


@pytest.mark.asyncio
async def test_conpty_registry_close_retries_unconfirmed_termination() -> None:
    runtime = build_terminal_runtime(
        get_state_store(), workspace_root=Path.cwd()
    )
    await runtime.start()
    process = _ConPtyProcess(fail_first_close=True)
    lease = _Lease()
    session = _ConPtySession(
        shell_id="retry",
        process=process,
        lease=cast(Any, lease),
        registry=runtime.conpty,
        cwd=Path("."),
        command="cmd.exe",
        created=1,
    )
    with runtime.conpty.lock:
        runtime.conpty.sessions[session.shell_id] = session

    with pytest.raises(RuntimeError, match="failed to close"):
        await runtime.aclose()

    assert runtime.conpty.sessions == {"retry": session}
    assert process.close_calls == 1
    assert lease.released is False

    await runtime.aclose()

    assert process.close_calls == 2
    assert runtime.conpty.sessions == {}
    assert lease.released is True
