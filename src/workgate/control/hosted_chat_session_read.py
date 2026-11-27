"""Read-only Hosted Chat access to existing owned native Sessions.

The final upstream registry's read classification and live authorization
recheck are adapted to Workgate's native catalog and Session identities.
"""

import asyncio
import copy
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.types import CallToolResult
from pydantic import TypeAdapter

from ..hosted_chat.durable_tasks import DurableTaskRepository
from ..hosted_chat.generation_actor_lease import ActorLease
from ..hosted_chat.provider_common import ModelToolCall, complete_tool_batch
from ..protocol.ids import SessionId
from .hosted_chat_native_creation import _grant, _registered
from .mcp.app import build_mcp
from .runtime import ControlRuntime

# Explicit source-derived classifications, not MCP annotation heuristics.
SESSION_FILE_READ_TOOLS = frozenset(
    {"read", "list_files", "tree_view", "glob_search", "search"}
)
_AUTHORIZATION_RECHECK_SECONDS = 0.5


async def _await_authorized[T](
    invocation: Awaitable[T], check_authorization: Callable[[], None]
) -> T:
    """Recheck authorization while awaiting native work and before disclosure."""
    task = asyncio.ensure_future(invocation)
    try:
        while True:
            done, _ = await asyncio.wait(
                {task}, timeout=_AUTHORIZATION_RECHECK_SECONDS
            )
            check_authorization()
            if done:
                return task.result()
    except BaseException:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise


class NativeSessionReadDispatcher:
    """Reuse native MCP validation, routing, watchdog, and result projection."""

    def __init__(self, runtime: ControlRuntime) -> None:
        self._runtime = runtime
        self._mcp = build_mcp(runtime=runtime, audit_transport="hosted-chat")

    async def dispatch(
        self,
        repository: DurableTaskRepository,
        lease: ActorLease,
        call: ModelToolCall,
        *,
        machine: str,
        scopes: set[str],
        expires: int | None,
    ) -> dict[str, Any]:
        runtime = self._runtime
        if repository.database.root != runtime.config.state_dir / "hosted-chat":
            raise PermissionError(
                "Native intent belongs to another Control runtime"
            )
        scopes, expires = _grant(runtime, lease.owner, scopes, expires)
        call = copy.deepcopy(call)
        complete_tool_batch([call])
        if call.name not in SESSION_FILE_READ_TOOLS:
            raise PermissionError(
                "Native tool is not an authorized Session file read"
            )
        session_id = TypeAdapter(SessionId).validate_python(
            call.arguments.get("session_id"), strict=True
        )

        def authorize() -> None:
            repository.authorize_session_read(
                lease,
                session_id,
                machine=machine,
                scopes=scopes,
                expires=expires,
                registered=_registered(runtime),
            )
            record = runtime.session_coordinator.require_session_status(
                session_id, {"active"}
            )
            if str(record.executor_id) != machine:
                raise PermissionError("Native Session binding mismatch")

        authorize()
        result = await _await_authorized(
            self._mcp.call_tool(call.name, call.arguments), authorize
        )
        if not isinstance(result, CallToolResult):
            raise RuntimeError("Unexpected interactive native tool result")
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)
