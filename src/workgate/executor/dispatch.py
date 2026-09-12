"""Executor-local operation dispatch for final protocol commands."""

from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Any

from ..tools.machine import (
    EXECUTOR_AGENT_MCP_OPERATION_NAMES,
    MACHINE_TOOL_NAMES,
)

EXECUTOR_OPERATION_NAMES = (
    MACHINE_TOOL_NAMES | EXECUTOR_AGENT_MCP_OPERATION_NAMES | {"job"}
)

type ExecutorHandler = Callable[[dict[str, Any]], Awaitable[Any]]


def _requires_composed_services(op: str) -> ExecutorHandler:
    """Return the common fail-closed placeholder used before composition."""

    async def handler(args: dict[str, Any]) -> Any:
        del args
        raise RuntimeError(f"{op} requires composed executor services")

    return handler


async def _open_terminal_bridge(args: dict[str, Any]) -> Any:
    from workgate.executor.terminal.bridge import open_terminal_bridge_execute

    return await open_terminal_bridge_execute(
        str(args["shell_id"]),
        int(args.get("cols") or 120),
        int(args.get("rows") or 36),
    )


async def _read_terminal_bridge(args: dict[str, Any]) -> Any:
    from workgate.executor.terminal.bridge import read_terminal_bridge_execute

    return await read_terminal_bridge_execute(
        str(args["bridge_id"]),
        int(args.get("max_bytes") or 65_536),
        int(args.get("wait_ms") or 0),
    )


async def _write_terminal_bridge(args: dict[str, Any]) -> Any:
    from workgate.executor.terminal.bridge import write_terminal_bridge_execute

    return await write_terminal_bridge_execute(
        str(args["bridge_id"]),
        str(args.get("data_b64") or ""),
    )


async def _resize_terminal_bridge(args: dict[str, Any]) -> Any:
    from workgate.executor.terminal.bridge import resize_terminal_bridge_execute

    return await resize_terminal_bridge_execute(
        str(args["bridge_id"]),
        int(args["cols"]),
        int(args["rows"]),
    )


async def _close_terminal_bridge(args: dict[str, Any]) -> Any:
    from workgate.executor.terminal.bridge import close_terminal_bridge_execute

    return await close_terminal_bridge_execute(str(args["bridge_id"]))


_DIRECT_EXECUTOR_HANDLERS: Mapping[str, ExecutorHandler] = MappingProxyType(
    {
        "close_terminal_bridge": _close_terminal_bridge,
        "open_terminal_bridge": _open_terminal_bridge,
        "read_terminal_bridge": _read_terminal_bridge,
        "resize_terminal_bridge": _resize_terminal_bridge,
        "write_terminal_bridge": _write_terminal_bridge,
    }
)

_DEFAULT_EXECUTOR_HANDLERS: Mapping[str, ExecutorHandler] = MappingProxyType(
    {
        **{
            op: _requires_composed_services(op)
            for op in EXECUTOR_OPERATION_NAMES
            - _DIRECT_EXECUTOR_HANDLERS.keys()
        },
        **_DIRECT_EXECUTOR_HANDLERS,
    }
)

if frozenset(_DEFAULT_EXECUTOR_HANDLERS) != EXECUTOR_OPERATION_NAMES:
    missing = sorted(
        EXECUTOR_OPERATION_NAMES - _DEFAULT_EXECUTOR_HANDLERS.keys()
    )
    extra = sorted(_DEFAULT_EXECUTOR_HANDLERS.keys() - EXECUTOR_OPERATION_NAMES)
    raise RuntimeError(
        f"executor handler/tool classification mismatch: missing={missing}, extra={extra}"
    )


class ExecutorDispatcher:
    """Immutable executor operation table with no control-plane compatibility policy."""

    def __init__(self, handlers: Mapping[str, ExecutorHandler]) -> None:
        resolved = dict(handlers)
        names = frozenset(resolved)
        if names != EXECUTOR_OPERATION_NAMES:
            missing = sorted(EXECUTOR_OPERATION_NAMES - names)
            extra = sorted(names - EXECUTOR_OPERATION_NAMES)
            raise ValueError(
                f"executor handler/tool classification mismatch: missing={missing}, extra={extra}"
            )
        self._handlers = MappingProxyType(resolved)

    @property
    def handlers(self) -> Mapping[str, ExecutorHandler]:
        return self._handlers

    async def execute(self, op: str, args: dict[str, Any]) -> Any:
        try:
            handler = self._handlers[op]
        except KeyError as exc:
            raise ValueError(f"unsupported executor operation: {op}") from exc
        return await handler(dict(args or {}))


def build_executor_dispatcher(
    *, handler_overrides: Mapping[str, ExecutorHandler] | None = None
) -> ExecutorDispatcher:
    """Build one executor-local dispatcher with optional known-handler overrides."""
    overrides = dict(handler_overrides or {})
    unknown = set(overrides) - EXECUTOR_OPERATION_NAMES
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unknown executor handler override: {names}")
    return ExecutorDispatcher({**_DEFAULT_EXECUTOR_HANDLERS, **overrides})


async def execute_executor_tool(op: str, args: dict[str, Any]) -> Any:
    """Execute one executor operation through a fresh default dispatcher."""
    return await build_executor_dispatcher().execute(op, args)
