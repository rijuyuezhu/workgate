import asyncio

import pytest

from workgate.ui.http.live_state import build_human_ui_runtime


@pytest.mark.asyncio
async def test_human_ui_runtime_owns_only_its_connection_registry() -> None:
    outer = build_human_ui_runtime()
    inner = build_human_ui_runtime()

    await outer.start()
    await outer.start()
    await inner.start()
    try:
        outer_marker = outer.terminal_connections.reserve(1)
        inner_marker = inner.terminal_connections.reserve(1)
        assert outer_marker is not None
        assert inner_marker is not None
        assert outer.terminal_connections.active_count() == 1
        assert inner.terminal_connections.active_count() == 1
        outer.terminal_connections.release(outer_marker)
        inner.terminal_connections.release(inner_marker)
    finally:
        await inner.aclose()
        await outer.aclose()

    with pytest.raises(RuntimeError, match="cannot be restarted"):
        await outer.start()


@pytest.mark.asyncio
async def test_terminal_connection_shutdown_cancels_owner_task() -> None:
    runtime = build_human_ui_runtime()
    await runtime.start()
    entered = asyncio.Event()

    async def connection_owner() -> None:
        marker = runtime.terminal_connections.reserve(1)
        assert marker is not None
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            runtime.terminal_connections.release(marker)

    task = asyncio.create_task(connection_owner())
    await entered.wait()
    assert runtime.terminal_connections.active_count() == 1

    await runtime.aclose()

    assert task.cancelled()
    assert runtime.terminal_connections.active_count() == 0
    assert runtime.terminal_connections.reserve(1) is None
    await runtime.aclose()


@pytest.mark.asyncio
async def test_human_ui_runtime_closes_terminal_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_human_ui_runtime()
    await runtime.start()
    events: list[str] = []
    terminal_close = runtime.terminal_connections.aclose

    async def close_terminals() -> None:
        events.append("terminals")
        await terminal_close()

    monkeypatch.setattr(runtime.terminal_connections, "aclose", close_terminals)

    await runtime.aclose()

    assert events == ["terminals"]


@pytest.mark.asyncio
async def test_human_ui_start_failure_closes_runtime_and_prevents_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_human_ui_runtime()

    async def fail_start() -> None:
        raise RuntimeError("terminal start failed")

    monkeypatch.setattr(runtime.terminal_connections, "start", fail_start)

    with pytest.raises(RuntimeError, match="terminal start failed"):
        await runtime.start()

    assert runtime._closed is True
    assert runtime.terminal_connections.reserve(1) is None
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        await runtime.start()
