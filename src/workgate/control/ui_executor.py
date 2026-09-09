"""Narrow control-side routing for Human UI operations owned by executors."""

from typing import Any, cast

from pydantic import JsonValue

from ..errors import exception_from_tool_error
from .runtime_types import ControlRuntimeLike


async def resolve_ui_executor(
    runtime: ControlRuntimeLike, executor_id: str | None
) -> str:
    """Resolve an explicit or uniquely eligible executor for a Human UI call."""
    return await runtime.session_coordinator.select_executor(executor_id)


async def call_ui_executor(
    runtime: ControlRuntimeLike,
    executor_id: str | None,
    op: str,
    args: dict[str, JsonValue] | None = None,
) -> tuple[str, JsonValue]:
    """Call one explicitly named internal UI operation on an eligible executor."""
    resolved_executor_id = await resolve_ui_executor(runtime, executor_id)
    result = await runtime.executor_transport.call(
        resolved_executor_id, op, args or {}
    )
    if result.ok:
        return resolved_executor_id, result.result
    assert result.error is not None
    if result.error.data is not None:
        raise exception_from_tool_error(cast(dict[str, Any], result.error.data))
    raise RuntimeError(f"{result.error.code}: {result.error.message}")
