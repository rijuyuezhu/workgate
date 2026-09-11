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


def test_executor_dispatcher_requires_exact_handler_set() -> None:
    dispatcher = build_executor_dispatcher()
    handlers = dict(dispatcher.handlers)
    handlers.pop(next(iter(handlers)))

    with pytest.raises(
        ValueError, match="executor handler/tool classification mismatch"
    ):
        ExecutorDispatcher(handlers)
