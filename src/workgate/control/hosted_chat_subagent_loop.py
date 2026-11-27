"""Private foreground child runner over the existing durable model loop."""

import copy
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace

from ..hosted_chat.agent_runtime import (
    AgentContext,
    AgentLoopState,
    RuntimeSafetyError,
    run_agent_loop,
)
from ..hosted_chat.durable_tasks import DurableTaskRepository
from ..hosted_chat.graph_topology import graph_members
from ..hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from ..hosted_chat.provider_common import ModelToolCall
from ..hosted_chat.providers import stream_model, stream_model_with_tools
from ..hosted_chat.subagent_scope import SubagentScope
from ..hosted_chat.subagents import (
    MAX_SUBAGENT_DEPTH,
    SubagentConfig,
    SubagentInvocation,
    invoke_subagent,
    subagent_tools,
)
from .hosted_chat_native_tool_node import NativeLoopToolNode


class ForegroundDelegation:
    """Trusted opt-in; advertise only single-Agent foreground configurations."""

    def __init__(
        self,
        configs: list[SubagentConfig],
        resolve_context: Callable[[dict], Awaitable[AgentContext]],
        *,
        max_depth: int = MAX_SUBAGENT_DEPTH,
        stream_tools=stream_model_with_tools,
        stream_text=stream_model,
    ):
        self.configs = [
            copy.deepcopy(config)
            for config in configs
            if max_depth > 0
            and (
                config.ordinary_chat
                or (
                    len(graph_members(config.agent)) == 1
                    and not config.agent.get("edges")
                )
            )
        ]
        self.resolve_context = resolve_context
        self.max_depth = max_depth
        self.stream_tools = stream_tools
        self.stream_text = stream_text

    def definitions(self) -> list[dict]:
        return subagent_tools(self.configs) if self.configs else []

    def validate(self, call: ModelToolCall) -> None:
        if (
            not isinstance(call.arguments, dict)
            or set(call.arguments) - {"intent", "description", "subagent_type"}
            or any(
                not isinstance(value, str) for value in call.arguments.values()
            )
            or call.arguments.get("subagent_type")
            not in {c.type for c in self.configs}
        ):
            raise PermissionError(
                "Subagent call is not an advertised foreground delegation"
            )

    @staticmethod
    def authorize_receipt(
        node: NativeLoopToolNode, call: ModelToolCall, record: dict
    ) -> None:
        scope = SubagentScope.from_record(record)
        checkpoint = node.checkpoint
        lease = checkpoint.lease
        if (
            scope.parent_scope != checkpoint.scope
            or scope.parent_checkpoint_id
            != checkpoint.active["configurable"].get("checkpoint_id")
            or scope.call_id != call.call_id
        ):
            raise PermissionError(
                "Subagent receipt belongs to a different parent edge"
            )
        scopes, expiry = node.current_grant()
        with lease.database.read_snapshot() as db:
            scope.resolve(db, lease)
            scope.effective_grant(scopes, expiry)
            row = db.execute(
                "SELECT state FROM runs WHERE id=?", (lease.run_id,)
            ).fetchone()
            # A final child result may contain information from completed
            # descendants. Their parent edges advanced, but their original
            # grants and owned revisions must still be valid.
            for value in (
                json.loads(row["state"]).get("ownedSubagentScopes", {}).values()
            ):
                descendant = SubagentScope.from_record(value)
                parent = descendant.parent_scope
                while isinstance(parent, SubagentScope):
                    if parent == scope:
                        descendant.resolve_owned(db, lease.owner, lease.run_id)
                        descendant.effective_grant(scopes, expiry)
                        break
                    parent = parent.parent_scope

    async def invoke(
        self,
        node: NativeLoopToolNode,
        call: ModelToolCall,
        context: AgentContext,
        authorize_effects: Callable[
            [list[ModelToolCall], AgentLoopState], Awaitable[None]
        ]
        | None,
    ) -> tuple[dict, dict]:
        self.validate(call)
        lease = node.checkpoint.lease
        admitted: SubagentScope | None = None

        async def execute_child(invocation: SubagentInvocation) -> list[dict]:
            nonlocal admitted
            scope = SubagentScope.admit(
                node.checkpoint,
                call,
                invocation,
                disclosure_access=node._receipt_access(),
            )
            admitted = scope
            child_checkpoint = OwnedLoopCheckpoint(lease, scope=scope)
            resolved = (
                context
                if invocation.ordinary_chat
                else await self.resolve_context(copy.deepcopy(invocation.agent))
            )
            child_context = replace(
                resolved,
                agent=None
                if invocation.ordinary_chat
                else copy.deepcopy(invocation.agent),
                tools=[],
                parameters=copy.deepcopy(resolved.parameters),
                recursion_limit=min(
                    (
                        limit
                        for limit in (
                            resolved.recursion_limit,
                            invocation.recursion_limit,
                        )
                        if limit is not None
                    ),
                    default=None,
                ),
                compaction_semantic_index=None,
                fading_tier=None,
            )

            def check_parent() -> None:
                node.current_grant()

            child_node = NativeLoopToolNode(
                node.runtime,
                DurableTaskRepository(lease.database, scope=scope),
                child_checkpoint,
                allowed_tools=node.allowed_tools,
                scopes=set(scope.scopes),
                expires=scope.grant_expires,
                task_machine=node.task_machine,
                check_grant=check_parent,
                delegation=ForegroundDelegation(
                    list(invocation.child_configs),
                    self.resolve_context,
                    max_depth=scope.remaining_depth,
                    stream_tools=self.stream_tools,
                    stream_text=self.stream_text,
                ),
            )
            recovered = child_checkpoint.restore(child_context)
            if recovered is not None and (
                recovered.partial_text or recovered.public_delta_cursor
            ):
                raise RuntimeSafetyError(
                    "Foreground child checkpoint contains public output"
                )
            state = (
                recovered.state
                if recovered
                else AgentLoopState(
                    child_context, copy.deepcopy(invocation.messages)
                )
            )

            async def discard(_value: str) -> None:
                child_node.current_grant()

            async def check_stream() -> bool:
                child_node.current_grant()
                return False

            async def save(
                state: AgentLoopState, calls: list[ModelToolCall]
            ) -> None:
                child_node.current_grant()
                await child_checkpoint.save(state, calls)

            async def dispatch(
                calls: list[ModelToolCall], state: AgentLoopState
            ):
                return await child_node.dispatch(
                    calls, state, authorize_effects=authorize_effects
                )

            child_node.current_grant()
            await run_agent_loop(
                state,
                stream_tools=self.stream_tools,
                stream_text=self.stream_text,
                dispatch=dispatch,
                on_text=discard,
                on_thought=discard,
                save_state=save,
                before_model=child_node.refresh_tools,
                preflight_calls=child_node.preflight,
                on_stream_chunk=check_stream,
            )
            child_node.current_grant()
            return state.messages

        result = await invoke_subagent(
            call,
            self.configs,
            execute_child=execute_child,
            current_grant=node.current_grant,
            owner=lease.owner,
            conversation_id=node.checkpoint.active["configurable"]["thread_id"],
            parent_run_id=lease.run_id,
            database=lease.database,
            parent_agent_id=(context.agent or {}).get("id"),
            max_depth=self.max_depth,
        )
        if admitted is None:
            raise RuntimeSafetyError("Foreground child failed before admission")
        record = admitted.record()
        self.authorize_receipt(node, call, record)
        return {"content": [{"type": "text", "text": result}]}, record
