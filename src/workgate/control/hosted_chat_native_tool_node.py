"""Private native tool node for the checkpointed Hosted Chat Agent loop."""

import copy
import time
from collections.abc import Callable, Collection

from ..hosted_chat.agent_runtime import AgentLoopState, ToolBatchOutput
from ..hosted_chat.durable_tasks import DurableTaskRepository
from ..hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from ..hosted_chat.loop_receipts import execute_checkpointed_batch
from ..hosted_chat.mcp_projection import native_tool_history_message
from ..hosted_chat.provider_common import ModelToolCall, complete_tool_batch
from .hosted_chat_native_creation import _grant
from .hosted_chat_native_dispatch import NativeToolDispatcher, _selected_tools
from .runtime import ControlRuntime


class NativeLoopToolNode:
    """Compose native dispatch and durable replay without a generic MCP fallback.

    The foreground caller must supply the live principal/grant check and run
    the approval/content-policy hooks before dispatching the tool node.
    """

    def __init__(
        self,
        runtime: ControlRuntime,
        repository: DurableTaskRepository,
        checkpoint: OwnedLoopCheckpoint,
        *,
        allowed_tools: Collection[str],
        scopes: set[str],
        expires: int | None,
        task_machine: str | None,
        check_grant: Callable[[], None],
    ) -> None:
        self.runtime = runtime
        self.repository = repository
        self.checkpoint = checkpoint
        self.allowed_tools = frozenset(allowed_tools)
        self.scopes = frozenset(scopes)
        self.expires = expires
        self.task_machine = task_machine
        self._check_grant = check_grant
        if (
            repository.database.path != checkpoint.lease.database.path
            or repository.database.root
            != runtime.config.state_dir / "hosted-chat"
        ):
            raise PermissionError(
                "Native node does not belong to checkpoint database"
            )

        self.native = NativeToolDispatcher(runtime)

    def _check(self) -> tuple[set[str], int | None]:
        self.checkpoint.lease.assert_current()
        self._check_grant()
        scopes, expires = _grant(
            self.runtime,
            self.checkpoint.lease.owner,
            set(self.scopes),
            self.expires,
        )
        if expires is not None and expires <= time.time():
            raise PermissionError("Native tool grant expired")
        if not self.scopes.issubset(scopes):
            raise PermissionError("Native tool principal grant was reduced")
        return scopes, expires

    async def refresh_tools(self, state: AgentLoopState) -> None:
        scopes, expires = self._check()
        state.context.tools = await self.native.definitions(
            self.repository,
            self.checkpoint.lease,
            allowed_tools=self.allowed_tools,
            scopes=scopes,
            expires=expires,
            task_machine=self.task_machine,
        )
        self._check()

    def preflight(
        self, calls: list[ModelToolCall], state: AgentLoopState
    ) -> list[ModelToolCall]:
        self._check()
        calls = copy.deepcopy(complete_tool_batch(calls))
        shown = {tool["function"]["name"] for tool in state.context.tools}
        if any(call.name not in shown for call in calls):
            raise PermissionError("Native tool was not advertised")
        prepared = self.native.prepare_model_batch(
            self.repository,
            self.checkpoint.lease,
            calls,
            allowed_tools=self.allowed_tools,
            task_machine=self.task_machine,
        )
        return [call for call, _ in prepared]

    async def dispatch(
        self, calls: list[ModelToolCall], state: AgentLoopState
    ) -> ToolBatchOutput:
        calls = copy.deepcopy(complete_tool_batch(calls))

        def check() -> None:
            self._check()
            _, agent = self.repository.tool_context(self.checkpoint.lease)
            enabled = _selected_tools(agent, self.allowed_tools)
            if any(call.name not in enabled for call in calls):
                raise PermissionError("Native tool no longer selected")

        async def execute(call: ModelToolCall) -> dict:
            scopes, expires = self._check()
            return await self.native.dispatch_model_call(
                self.repository,
                self.checkpoint.lease,
                call,
                allowed_tools=self.allowed_tools,
                scopes=scopes,
                expires=expires,
                task_machine=self.task_machine,
            )

        def preflight_unsettled(unsettled: list[ModelToolCall]) -> None:
            self.native.prepare_model_batch(
                self.repository,
                self.checkpoint.lease,
                unsettled,
                allowed_tools=self.allowed_tools,
                task_machine=self.task_machine,
            )

        outputs = await execute_checkpointed_batch(
            self.checkpoint,
            calls,
            execute,
            check_grant=check,
            preflight_unsettled=preflight_unsettled,
        )
        return ToolBatchOutput(
            [
                native_tool_history_message(
                    call.call_id, output, state.context.profile.provider
                )
                for call, output in zip(calls, outputs, strict=True)
            ]
        )
