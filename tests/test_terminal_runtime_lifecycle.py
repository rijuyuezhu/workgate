"""Executor-owned terminal bridge registry lifecycle on POSIX."""

import pytest

import workgate.executor.terminal.bridge as bridge_module
from workgate.executor.terminal.runtime import (
    build_terminal_runtime,
    use_terminal_runtime,
)
from workgate.persistence import get_state_store


@pytest.mark.asyncio
async def test_terminal_runtime_context_nested_and_reversible():
    outer = build_terminal_runtime(get_state_store())
    inner = build_terminal_runtime(get_state_store())
    await outer.start()
    await inner.start()
    try:
        with use_terminal_runtime(outer):
            assert bridge_module._bridge_registry() is outer.bridges
            with use_terminal_runtime(inner):
                assert bridge_module._bridge_registry() is inner.bridges
            assert bridge_module._bridge_registry() is outer.bridges
    finally:
        await inner.aclose()
        await outer.aclose()
    with pytest.raises(RuntimeError, match="execution context"):
        bridge_module._bridge_registry()


@pytest.mark.asyncio
async def test_terminal_runtime_start_and_close_idempotent():
    runtime = build_terminal_runtime(get_state_store())
    await runtime.start()
    await runtime.start()
    await runtime.aclose()
    await runtime.aclose()
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        await runtime.start()


@pytest.mark.asyncio
async def test_terminal_runtime_lifespan_closes_bridges():
    runtime = build_terminal_runtime(get_state_store())
    async with runtime.lifespan():
        with use_terminal_runtime(runtime):
            assert bridge_module._bridge_registry() is runtime.bridges
    assert runtime._close_complete


@pytest.mark.asyncio
async def test_terminal_runtime_start_failure_closes_admission(monkeypatch):
    runtime = build_terminal_runtime(get_state_store())

    async def fail_start():
        raise RuntimeError("bridge startup failed")

    monkeypatch.setattr(runtime.bridges, "start", fail_start)
    with pytest.raises(RuntimeError, match="bridge startup failed"):
        await runtime.start()
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        await runtime.start()


@pytest.mark.asyncio
async def test_terminal_runtime_stops_bridge_admission_before_close(
    monkeypatch,
):
    runtime = build_terminal_runtime(get_state_store())
    events = []
    original_stop = runtime.bridges.stop_admission
    original_close = runtime.bridges.aclose

    def stop_admission():
        events.append("stop")
        original_stop()

    async def close_bridges():
        events.append("close")
        await original_close()

    monkeypatch.setattr(runtime.bridges, "stop_admission", stop_admission)
    monkeypatch.setattr(runtime.bridges, "aclose", close_bridges)
    await runtime.start()
    await runtime.aclose()
    assert events[:2] == ["stop", "close"]
