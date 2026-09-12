import asyncio

import pytest

from workgate.ui.http.live_state import build_human_ui_runtime, human_ui_runtime


@pytest.mark.asyncio
async def test_human_ui_runtime_installs_and_restores_nested_bindings() -> None:
    outer = build_human_ui_runtime()
    inner = build_human_ui_runtime()

    await outer.start()
    try:
        assert human_ui_runtime() is outer
        await outer.start()

        await inner.start()
        try:
            assert human_ui_runtime() is inner
        finally:
            await inner.aclose()

        assert human_ui_runtime() is outer
        await inner.aclose()
    finally:
        await outer.aclose()

    with pytest.raises(RuntimeError, match="not configured"):
        human_ui_runtime()
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
async def test_human_ui_runtime_closes_terminal_registry_and_restores_binding(
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
    with pytest.raises(RuntimeError, match="not configured"):
        human_ui_runtime()
