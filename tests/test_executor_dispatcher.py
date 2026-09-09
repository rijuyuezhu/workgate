from __future__ import annotations

from typing import Any

import pytest

from workgate.executor.dispatch import (
    EXECUTOR_OPERATION_NAMES,
    ExecutorDispatcher,
    build_executor_dispatcher,
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


def test_executor_dispatcher_requires_exact_handler_set() -> None:
    dispatcher = build_executor_dispatcher()
    handlers = dict(dispatcher.handlers)
    handlers.pop(next(iter(handlers)))

    with pytest.raises(
        ValueError, match="executor handler/tool classification mismatch"
    ):
        ExecutorDispatcher(handlers)
