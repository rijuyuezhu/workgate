"""MCP tool audit and timeout watchdog helpers."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, Icon, ToolAnnotations

from ...audit import (
    audit,
    audit_call_context,
    audit_tool_call_end,
    audit_tool_call_start,
    new_audit_call_id,
)
from ...config.control import ControlConfig
from ...errors import public_error_type
from ...jobs.managed import ManagedJobsRuntime
from ...oauth.core.state import OAuthState
from ...persistence import StateStore
from ...tools.declarative import mcp_handler_error_handler
from ...tools.mcp_text import has_explicit_tool_text
from ...tools.metadata import tool_safety_annotations
from ...utils.serialization import to_jsonable
from ..execution_context import control_execution_context
from ..tool_timeouts import SHELL_COMMAND_TOOL_NAMES, tool_timeout_s


class PublicToolTimeoutError(TimeoutError):
    """Signals that a public tool timed out and should return structured retry guidance instead of a generic failure."""

    pass


def _mcp_tool_input(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Represent MCPServer positional/keyword arguments as the routed tool input payload."""
    if kwargs and not args:
        return kwargs
    if args and not kwargs:
        return list(args)
    if args or kwargs:
        return {"args": list(args), "kwargs": kwargs}
    return {}


def _mcp_tool_is_app_only(meta: dict[str, Any] | None) -> bool:
    if not isinstance(meta, dict):
        return False
    ui = meta.get("ui")
    return isinstance(ui, dict) and ui.get("visibility") == ["app"]


def _mcp_tool_audit_watchdog_wrapper(
    original: Callable[..., Awaitable[Any]],
    tool_name: str,
    config: ControlConfig,
    state_store: StateStore,
    oauth_state: OAuthState | None,
    managed_jobs_runtime: ManagedJobsRuntime | None,
    agent_activity_observer: Callable[[tuple[str, ...]], Awaitable[None]]
    | None,
) -> Callable[..., Awaitable[Any]]:
    """Return a wrapper that audits every MCP tool call and enforces the tool timeout."""

    mcp_error_handler = mcp_handler_error_handler(original)

    @wraps(original)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        with control_execution_context(
            config=config,
            state_store=state_store,
            oauth_state=oauth_state,
            managed_jobs_runtime=managed_jobs_runtime,
        ):
            return await _wrapped_with_state(*args, **kwargs)

    async def _wrapped_with_state(*args: Any, **kwargs: Any) -> Any:
        call_id = new_audit_call_id()
        start = time.time()
        tool_input = _mcp_tool_input(args, kwargs)
        session_ids, task_ids = audit_tool_call_start(
            call_id=call_id,
            transport="mcp",
            tool=tool_name,
            input=tool_input,
        )
        timeout_s = (
            tool_timeout_s(tool_name, args=tool_input)
            if tool_name in SHELL_COMMAND_TOOL_NAMES
            and isinstance(tool_input, dict)
            else tool_timeout_s(tool_name)
        )
        try:
            if agent_activity_observer is not None and task_ids:
                await agent_activity_observer(task_ids)
            with audit_call_context(call_id, session_ids, task_ids):
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
                # Let MCPServer report the timeout as a tool execution error.
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
                task_ids=task_ids,
            )
            if payload is None:
                raise ToolError(str(exc)) from None
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
                task_ids=task_ids,
            )
            raise
        duration_ms = int((time.time() - start) * 1000)
        audit_output = (
            result.structured_content
            if isinstance(result, CallToolResult)
            and result.structured_content is not None
            and has_explicit_tool_text(tool_name)
            else result
        )
        audit_tool_call_end(
            call_id=call_id,
            transport="mcp",
            tool=tool_name,
            ok=True,
            duration_ms=duration_ms,
            output=to_jsonable(audit_output),
            session_ids=session_ids,
            task_ids=task_ids,
        )
        return result

    if hasattr(original, "__signature__"):
        wrapped.__signature__ = original.__signature__  # type: ignore[attr-defined]
    return wrapped


class WorkgateMCPServer(MCPServer):
    """Register audited tools using only the SDK's public add_tool API."""

    def __init__(
        self,
        *,
        config: ControlConfig,
        state_store: StateStore,
        oauth_state: OAuthState | None,
        managed_jobs_runtime: ManagedJobsRuntime | None,
        agent_activity_observer: Callable[[tuple[str, ...]], Awaitable[None]]
        | None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._workgate_config = config
        self._workgate_state_store = state_store
        self._workgate_oauth_state = oauth_state
        self._workgate_managed_jobs_runtime = managed_jobs_runtime
        self._workgate_agent_activity_observer = agent_activity_observer

    def add_tool(
        self,
        fn: Callable[..., Any],
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list[Icon] | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
    ) -> None:
        tool_name = name or fn.__name__
        read_only = bool(annotations and annotations.read_only_hint)
        observer = (
            None
            if _mcp_tool_is_app_only(meta)
            else self._workgate_agent_activity_observer
        )
        super().add_tool(
            _mcp_tool_audit_watchdog_wrapper(
                fn,
                tool_name,
                self._workgate_config,
                self._workgate_state_store,
                self._workgate_oauth_state,
                self._workgate_managed_jobs_runtime,
                observer,
            ),
            name=tool_name,
            title=title,
            description=description,
            annotations=tool_safety_annotations(tool_name, read_only=read_only),
            icons=icons,
            meta=meta,
            structured_output=structured_output,
        )
