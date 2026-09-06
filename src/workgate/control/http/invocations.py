"""HTTP transport invocation helpers for local tools."""

import time
from typing import Any

from ...audit import (
    audit_call_context,
    audit_tool_call_end,
    audit_tool_call_start,
    new_audit_call_id,
)
from ...tools.contracts import ToolHandler
from ...tools.local_handlers import call_local_tool
from ...utils.serialization import to_jsonable


async def call_http_tool(
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    handler: ToolHandler | None = None,
) -> Any:
    """Invoke a local tool from the REST API and audit the HTTP-routed call."""
    payload = args or {}
    call_id = new_audit_call_id()
    start = time.time()
    session_ids = audit_tool_call_start(
        call_id=call_id,
        transport="http",
        tool=tool_name,
        input=payload,
    )
    try:
        with audit_call_context(call_id, session_ids):
            result = (
                await handler(payload)
                if handler is not None
                else await call_local_tool(tool_name, payload)
            )
    except BaseException as exc:
        duration_ms = int((time.time() - start) * 1000)
        audit_tool_call_end(
            call_id=call_id,
            transport="http",
            tool=tool_name,
            ok=False,
            duration_ms=duration_ms,
            error={
                "type": type(exc).__name__,
                "message": str(exc),
                "repr": repr(exc),
            },
            session_ids=session_ids,
        )
        raise
    duration_ms = int((time.time() - start) * 1000)
    audit_tool_call_end(
        call_id=call_id,
        transport="http",
        tool=tool_name,
        ok=True,
        duration_ms=duration_ms,
        output=to_jsonable(result),
        session_ids=session_ids,
    )
    return result
