"""MCP tool audit and timeout watchdog helpers."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, Protocol, cast

from mcp.server.fastmcp import FastMCP

from ...audit import (
    audit,
    audit_call_context,
    audit_tool_call_end,
    audit_tool_call_start,
    new_audit_call_id,
)
from ...errors import public_error_type
from ...ops.shell import tool_timeout_s
from ...tools.declarative import mcp_handler_error_handler
from ...utils.serialization import to_jsonable


class AuditedMcpToolFn(Protocol):
    """Callable MCP tool wrapper marked after audit/watchdog installation."""

    __workgate_audit_watchdog__: bool

    def __call__(self, *args: Any, **kwargs: Any) -> Awaitable[Any]: ...


class PublicToolTimeoutError(TimeoutError):
    """Signals that a public tool timed out and should return structured retry guidance instead of a generic failure."""

    pass


def _mcp_tool_input(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Represent FastMCP positional/keyword arguments as the routed tool input payload."""
    if kwargs and not args:
        return kwargs
    if args and not kwargs:
        return list(args)
    if args or kwargs:
        return {"args": list(args), "kwargs": kwargs}
    return {}


def _mcp_tool_audit_watchdog_wrapper(
    original: Callable[..., Awaitable[Any]], tool_name: str
) -> AuditedMcpToolFn:
    """Return a wrapper that audits every MCP tool call and enforces the tool timeout."""

    mcp_error_handler = mcp_handler_error_handler(original)

    @wraps(original)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        call_id = new_audit_call_id()
        start = time.time()
        session_ids = audit_tool_call_start(
            call_id=call_id,
            transport="mcp",
            tool=tool_name,
            input=_mcp_tool_input(args, kwargs),
        )
        timeout_s = tool_timeout_s(tool_name)
        try:
            with audit_call_context(call_id, session_ids):
                result = await asyncio.wait_for(
                    original(*args, **kwargs), timeout=timeout_s
                )
        except TimeoutError:
            exc = PublicToolTimeoutError(
                f"{tool_name} exceeded {timeout_s} second tool timeout"
            )
            duration_ms = int((time.time() - start) * 1000)
            audit(
                "tool_timeout",
                tool=tool_name,
                timeout_s=timeout_s,
            )
            if mcp_error_handler is not None:
                payload = mcp_error_handler(exc, args, kwargs)
            else:
                # Let FastMCP report the timeout as a tool execution error.
                payload = None
            audit_tool_call_end(
                call_id=call_id,
                transport="mcp",
                tool=tool_name,
                ok=False,
                duration_ms=duration_ms,
                output=to_jsonable(payload),
                error={
                    "type": public_error_type(exc),
                    "message": str(exc),
                    "repr": repr(exc),
                },
                session_ids=session_ids,
            )
            if payload is None:
                raise exc from None
            return payload
        except BaseException as exc:
            duration_ms = int((time.time() - start) * 1000)
            audit_tool_call_end(
                call_id=call_id,
                transport="mcp",
                tool=tool_name,
                ok=False,
                duration_ms=duration_ms,
                error={
                    "type": public_error_type(exc),
                    "message": str(exc),
                    "repr": repr(exc),
                },
                session_ids=session_ids,
            )
            raise
        duration_ms = int((time.time() - start) * 1000)
        audit_tool_call_end(
            call_id=call_id,
            transport="mcp",
            tool=tool_name,
            ok=True,
            duration_ms=duration_ms,
            output=to_jsonable(result),
            session_ids=session_ids,
        )
        return result

    if hasattr(original, "__signature__"):
        wrapped.__signature__ = original.__signature__  # type: ignore[attr-defined]
    audited = cast(AuditedMcpToolFn, wrapped)
    audited.__workgate_audit_watchdog__ = True
    return audited


def install_mcp_tool_watchdogs(mcp: FastMCP) -> None:
    """Wrap FastMCP execution paths so public tools are audited and return structured timeout errors."""
    for tool in mcp._tool_manager._tools.values():
        if getattr(tool.fn, "__workgate_audit_watchdog__", False):
            continue
        tool.fn = _mcp_tool_audit_watchdog_wrapper(tool.fn, tool.name)
