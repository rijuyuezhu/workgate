from __future__ import annotations

from typing import Any

import pytest

from workgate.executor.dispatch import (
    EXECUTOR_OPERATION_NAMES,
    ExecutorDispatcher,
    build_executor_dispatcher,
    execute_executor_tool,
)
from workgate.tools.machine import (
    EXECUTOR_AGENT_MCP_OPERATION_NAMES,
    MACHINE_TOOL_NAMES,
)


def test_executor_dispatcher_membership_preserves_public_machine_boundary() -> (
    None
):
    dispatcher = build_executor_dispatcher()

    assert frozenset(dispatcher.handlers) == EXECUTOR_OPERATION_NAMES
    assert (
        {"job"} | EXECUTOR_AGENT_MCP_OPERATION_NAMES
    ) == EXECUTOR_OPERATION_NAMES - MACHINE_TOOL_NAMES


def test_executor_dispatchers_are_fresh_and_handler_maps_are_immutable() -> (
    None
):
    first = build_executor_dispatcher()
    second = build_executor_dispatcher()

    assert first is not second
    assert first.handlers is not second.handlers
    with pytest.raises(TypeError):
        first.handlers["search"] = first.handlers["search"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_executor_dispatcher_known_override_receives_isolated_args() -> (
    None
):
    observed: list[dict[str, Any]] = []

    async def bound_search(args: dict[str, Any]) -> dict[str, Any]:
        observed.append(args)
        args["mutated"] = True
        return {"query": args["query"]}

    dispatcher = build_executor_dispatcher(
        handler_overrides={"search": bound_search}
    )
    caller_args = {"query": "needle"}

    result = await dispatcher.execute("search", caller_args)

    assert result == {"query": "needle"}
    assert caller_args == {"query": "needle"}
    assert observed == [{"query": "needle", "mutated": True}]


def test_executor_dispatcher_rejects_unknown_override() -> None:
    async def handler(args: dict[str, Any]) -> None:
        del args

    with pytest.raises(ValueError, match="unknown executor handler override"):
        build_executor_dispatcher(
            handler_overrides={"not-an-operation": handler}
        )


@pytest.mark.asyncio
async def test_executor_dispatcher_rejects_unsupported_operation() -> None:
    dispatcher = build_executor_dispatcher()

    with pytest.raises(ValueError, match="unsupported executor operation"):
        await dispatcher.execute("not-an-operation", {})


@pytest.mark.asyncio
async def test_default_composed_executor_handler_fails_closed() -> None:
    dispatcher = build_executor_dispatcher()

    with pytest.raises(
        RuntimeError, match="search requires composed executor services"
    ):
        await dispatcher.execute("search", {"query": "needle"})


@pytest.mark.asyncio
async def test_default_dashboard_handler_requires_composed_executor_config() -> (
    None
):
    with pytest.raises(
        RuntimeError,
        match="dashboard_snapshot requires composed executor services",
    ):
        await execute_executor_tool("dashboard_snapshot", {})


@pytest.mark.asyncio
async def test_default_terminal_bridge_handler_applies_protocol_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workgate.executor.terminal.bridge as bridge

    observed: list[tuple[str, int, int]] = []

    async def open_bridge(shell_id: str, cols: int, rows: int) -> str:
        observed.append((shell_id, cols, rows))
        return "bridge"

    monkeypatch.setattr(bridge, "open_terminal_bridge_execute", open_bridge)

    assert (
        await execute_executor_tool(
            "open_terminal_bridge", {"shell_id": "shell-1"}
        )
        == "bridge"
    )
    assert observed == [("shell-1", 120, 36)]


@pytest.mark.asyncio
async def test_terminal_bridge_handlers_adapt_protocol_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workgate.executor.terminal.bridge as bridge

    observed: list[tuple[Any, ...]] = []

    async def read_bridge(bridge_id: str, max_bytes: int, wait_ms: int) -> str:
        observed.append(("read", bridge_id, max_bytes, wait_ms))
        return "read"

    async def write_bridge(bridge_id: str, data_b64: str) -> str:
        observed.append(("write", bridge_id, data_b64))
        return "write"

    async def resize_bridge(bridge_id: str, cols: int, rows: int) -> str:
        observed.append(("resize", bridge_id, cols, rows))
        return "resize"

    async def close_bridge(bridge_id: str) -> str:
        observed.append(("close", bridge_id))
        return "close"

    monkeypatch.setattr(bridge, "read_terminal_bridge_execute", read_bridge)
    monkeypatch.setattr(bridge, "write_terminal_bridge_execute", write_bridge)
    monkeypatch.setattr(bridge, "resize_terminal_bridge_execute", resize_bridge)
    monkeypatch.setattr(bridge, "close_terminal_bridge_execute", close_bridge)

    assert (
        await execute_executor_tool("read_terminal_bridge", {"bridge_id": "b1"})
        == "read"
    )
    assert (
        await execute_executor_tool(
            "write_terminal_bridge", {"bridge_id": "b1", "data_b64": "ZGF0YQ=="}
        )
        == "write"
    )
    assert (
        await execute_executor_tool(
            "resize_terminal_bridge",
            {"bridge_id": "b1", "cols": 90, "rows": 40},
        )
        == "resize"
    )
    assert (
        await execute_executor_tool(
            "close_terminal_bridge", {"bridge_id": "b1"}
        )
        == "close"
    )
    assert observed == [
        ("read", "b1", 65_536, 0),
        ("write", "b1", "ZGF0YQ=="),
        ("resize", "b1", 90, 40),
        ("close", "b1"),
    ]


def test_executor_dispatcher_requires_exact_handler_set() -> None:
    dispatcher = build_executor_dispatcher()
    handlers = dict(dispatcher.handlers)
    handlers.pop(next(iter(handlers)))

    with pytest.raises(
        ValueError, match="executor handler/tool classification mismatch"
    ):
        ExecutorDispatcher(handlers)
