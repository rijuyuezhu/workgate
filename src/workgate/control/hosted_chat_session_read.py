"""Read-only Hosted Chat access to existing owned native Sessions.

The final upstream registry's read classification and live authorization
recheck are adapted to Workgate's native catalog and Session identities.
"""

import asyncio
import copy
import json
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.types import CallToolResult, TextContent
from pydantic import TypeAdapter

from ..hosted_chat.durable_tasks import DurableTaskRepository
from ..hosted_chat.generation_actor_lease import ActorLease
from ..hosted_chat.provider_common import ModelToolCall, complete_tool_batch
from ..protocol.ids import ReservedShellId, SessionId
from ..schemas.result_models.shell import (
    ListPersistentShellsOutput,
    PersistentShellInfo,
    ReadPersistentShellOutput,
)
from .hosted_chat_native_creation import _grant, _registered
from .mcp.app import build_mcp
from .runtime import ControlRuntime

# Explicit source-derived classifications, not MCP annotation heuristics.
SESSION_FILE_READ_TOOLS = frozenset(
    {"read", "list_files", "tree_view", "glob_search", "search"}
)
SESSION_SHELL_READ_TOOLS = frozenset(
    {"read_persistent_shell_output", "list_persistent_shells"}
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
        if call.name not in SESSION_FILE_READ_TOOLS | SESSION_SHELL_READ_TOOLS:
            raise PermissionError(
                "Native tool is not an authorized Session read"
            )
        session_id = TypeAdapter(SessionId).validate_python(
            call.arguments.get("session_id"), strict=True
        )

        inspect_shells = call.name in SESSION_SHELL_READ_TOOLS
        shell_id = None
        if call.name == "read_persistent_shell_output":
            shell_id = TypeAdapter(ReservedShellId).validate_python(
                call.arguments.get("shell_id"),
                strict=True,
            )
        authorized_shells: frozenset[str] = frozenset()

        def authorize() -> None:
            nonlocal authorized_shells
            authorized_shells = repository.authorize_session_read(
                lease,
                session_id,
                machine=machine,
                scopes=scopes,
                expires=expires,
                registered=_registered(runtime),
                inspect_shells=inspect_shells,
            )
            if shell_id is not None and shell_id not in authorized_shells:
                raise PermissionError(
                    "Native Shell does not belong to this conversation"
                )
            record = runtime.session_coordinator.require_session_status(
                session_id, {"active"}
            )
            if str(record.executor_id) != machine:
                raise PermissionError("Native Session binding mismatch")

        authorize()
        initial_shells = authorized_shells
        result = await _await_authorized(
            self._mcp.call_tool(call.name, call.arguments), authorize
        )
        if not isinstance(result, CallToolResult):
            raise RuntimeError("Unexpected interactive native tool result")
        if inspect_shells:
            if result.is_error or result.structured_content is None:
                raise RuntimeError("Native Shell inspection was not confirmed")
            if shell_id is not None:
                read = ReadPersistentShellOutput.model_validate(
                    result.structured_content
                )
                if read.shell_id != shell_id:
                    raise RuntimeError(
                        "Native Shell receipt has a mismatched identity"
                    )
                projection = ReadPersistentShellOutput(
                    shell_id=read.shell_id,
                    output=read.output,
                    lines=read.lines,
                    backend=read.backend,
                ).model_dump(mode="json", exclude_none=True)
            else:
                listing = ListPersistentShellsOutput.model_validate(
                    result.structured_content
                )
                projection = ListPersistentShellsOutput(
                    shells=[
                        PersistentShellInfo(
                            shell_id=row.shell_id,
                            name=row.name,
                            cwd=row.cwd,
                            command=row.command,
                            backend=row.backend,
                        )
                        for row in listing.shells
                        if row.shell_id in initial_shells & authorized_shells
                    ]
                ).model_dump(mode="json", exclude_none=True)
            # The canonical native text may include unrelated Shell rows.
            # Rebuild it from the filtered typed projection only.
            result = CallToolResult(
                structured_content=projection,
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps(projection, ensure_ascii=False),
                    )
                ],
            )
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)
