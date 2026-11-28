"""Private graph driver using the existing native Agent loop for each activation."""

import asyncio
import copy
import time
from collections.abc import Awaitable, Callable, Collection
from contextlib import suppress
from dataclasses import replace

from ..hosted_chat.agent_runtime import (
    AgentContext,
    AgentLoopState,
    run_agent_loop,
)
from ..hosted_chat.detached_task_lease import DetachedTaskLease
from ..hosted_chat.durable_tasks import DurableTaskRepository
from ..hosted_chat.generation_actor_lease import ActorLease
from ..hosted_chat.generations import GenerationStore
from ..hosted_chat.graph_member_scope import GraphMemberScope
from ..hosted_chat.graph_runtime import (
    GraphActivation,
    GraphMemberResult,
    run_multi_agent_graph,
)
from ..hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from ..hosted_chat.provider_common import ModelToolCall
from ..hosted_chat.providers import stream_model, stream_model_with_tools
from ..hosted_chat.steering import SteeringStore, reconcile_applied_steers
from ..hosted_chat.subagent_scope import (
    DetachedExecutionScope,
    SubagentGraphScope,
    SubagentScope,
)
from .hosted_chat_foreground_loop import ObservableStreamInterrupted
from .hosted_chat_native_creation import _grant
from .hosted_chat_native_tool_node import NativeLoopToolNode
from .runtime import ControlRuntime


async def run_native_graph(
    runtime: ControlRuntime,
    lease: ActorLease,
    root_agent_id: str,
    history: list[dict],
    *,
    resolve_context: Callable[[dict], Awaitable[AgentContext]],
    authorize_batch: Callable[
        [list[ModelToolCall], AgentLoopState], Awaitable[None]
    ]
    | None,
    allowed_tools: Collection[str],
    scopes: set[str],
    expires: int | None,
    task_machine: str | None,
    check_grant: Callable[[], None],
    recursion_limit: int,
    stream_tools=stream_model_with_tools,
    stream_text=stream_model,
    result_only: bool = False,
    publish_output: bool = False,
    team_scope: SubagentScope | DetachedExecutionScope | None = None,
    team_root: dict | None = None,
    detached_controls: DetachedTaskLease | None = None,
    control_grant: Callable[[], dict] | None = None,
) -> list[dict]:
    """Bind StateGraph tasks to native effects without publishing member deltas.

    The trusted caller supplies provider resolution and content authorization.
    A graph activation has its own checkpoint, not its own public generation.
    Graph human interrupts use the SDK barrier. Foreground steering elects one member; public model-delta projection remains separate.
    """

    if lease.database.root != runtime.config.state_dir / "hosted-chat":
        raise PermissionError(
            "Graph generation belongs to another Control runtime"
        )
    if team_scope is None and authorize_batch is None:
        raise PermissionError("Foreground graph requires content authorization")
    if publish_output and team_scope is not None:
        raise PermissionError("Private team graph cannot publish public output")
    store = GenerationStore(lease.database)
    generation = (
        store.get(lease.owner, lease.run_id) if publish_output else None
    )
    if publish_output and generation is None:
        raise PermissionError("Graph generation is no longer owned")
    steering = SteeringStore(lease.database)
    if (team_scope is None) != (team_root is None):
        raise PermissionError("Team graph requires an admitted definition")
    if (detached_controls is None) != (control_grant is None) or (
        detached_controls is not None
        and (
            not isinstance(team_scope, DetachedExecutionScope)
            or detached_controls.task_id != team_scope.task_id
            or detached_controls.token != lease.token
            or detached_controls.database.path != lease.database.path
        )
    ):
        raise PermissionError("Detached graph control owner mismatch")
    scopes = set(scopes)
    allowed_tools = frozenset(allowed_tools)

    def check() -> None:
        lease.assert_current()
        check_grant()
        live_scopes, deadline = _grant(runtime, lease.owner, scopes, expires)
        if not scopes.issubset(live_scopes):
            raise PermissionError("Native graph principal grant was reduced")
        if deadline is not None and deadline <= time.time():
            raise PermissionError("Native graph grant expired")

    async def execute(activation: GraphActivation) -> GraphMemberResult:
        scope = activation.scope
        if team_scope is None and not isinstance(scope, GraphMemberScope):
            raise PermissionError(
                "Foreground graph requires its root activation scope"
            )
        if team_scope is not None and (
            not isinstance(scope, SubagentGraphScope)
            or scope.team != team_scope
        ):
            raise PermissionError(
                "Team graph requires its admitted member scope"
            )
        checkpoint = OwnedLoopCheckpoint(lease, scope=activation.scope)
        node = NativeLoopToolNode(
            runtime,
            DurableTaskRepository(lease.database, scope=activation.scope),
            checkpoint,
            activation=activation,
            allowed_tools=allowed_tools,
            scopes=scopes,
            expires=expires,
            task_machine=task_machine,
            check_grant=check,
        )
        check()
        context = await resolve_context(copy.deepcopy(activation.agent))
        node.current_grant()
        # Do not trust a resolver to provide arbitrary tools or another Agent.
        context = replace(
            context, agent=copy.deepcopy(activation.agent), tools=[]
        )
        context = replace(
            context,
            recursion_limit=min(recursion_limit, context.recursion_limit)
            if context.recursion_limit is not None
            else recursion_limit,
        )
        if team_scope is not None:
            context = replace(
                context, compaction_semantic_index=None, fading_tier=None
            )
        recovered = checkpoint.restore(context)
        state = (
            recovered.state
            if recovered is not None
            else AgentLoopState(context, copy.deepcopy(activation.history))
        )

        visible = publish_output and activation.visible_output
        partial_text = recovered.partial_text if recovered is not None else ""
        cursor = recovered.public_delta_cursor if recovered is not None else 0
        if visible:
            if (
                store.public_delta_cursor(
                    lease, activation=scope.checkpoint_namespace
                )
                != cursor
            ):
                raise ObservableStreamInterrupted(
                    "Uncheckpointed graph member delta"
                )
        elif partial_text or cursor:
            raise ObservableStreamInterrupted(
                "Private graph member contains public output"
            )

        async def emit(value: str, kind: str) -> None:
            nonlocal cursor, partial_text
            node.current_grant()
            if not visible or not value:
                return
            assert generation is not None
            part = {"type": kind, kind: value}
            if kind == "text":
                partial_text += value
                part["phase"] = "final_answer"
            cursor = store.append(
                lease.owner,
                lease.run_id,
                {
                    "event": "on_message_delta"
                    if kind == "text"
                    else "on_reasoning_delta",
                    "data": {
                        "id": generation.response_message_id,
                        "graphActivationId": scope.checkpoint_namespace,
                        "delta": {"content": part},
                    },
                },
                actor_lease=lease,
            )

        async def text(value: str) -> None:
            await emit(value, "text")

        async def thought(value: str) -> None:
            await emit(value, "think")

        async def stream_boundary() -> bool:
            node.current_grant()
            if detached_controls is not None:
                assert (
                    isinstance(scope, SubagentGraphScope)
                    and control_grant is not None
                )
                return detached_controls.has_interrupt(
                    member=scope, **control_grant()
                )
            return isinstance(scope, GraphMemberScope) and steering.has_preempt(
                lease.owner, lease.run_id
            )

        def reconcile(loop: AgentLoopState) -> bool:
            if not isinstance(scope, GraphMemberScope):
                return False
            added = reconcile_applied_steers(
                loop.messages,
                steering.applied_parts(lease, scope=scope),
            )
            if added:
                loop.completed = False
            return added

        async def boundary(loop: AgentLoopState, kind: str) -> bool:
            node.current_grant()
            if isinstance(scope, GraphMemberScope):
                if (
                    visible
                    and kind in {"stream", "terminal"}
                    and steering.pending(lease.owner, lease.run_id)
                ):
                    await save(loop, [])
                steering.drain(lease, boundary=kind, scope=scope)
            return reconcile(loop)

        async def save(
            loop: AgentLoopState, calls: list[ModelToolCall]
        ) -> None:
            node.current_grant()
            await checkpoint.save(
                loop,
                calls,
                partial_text=partial_text,
                public_delta_cursor=cursor,
            )
            if detached_controls is not None:
                assert (
                    isinstance(scope, SubagentGraphScope)
                    and control_grant is not None
                )
                detached_controls.acknowledge_controls(
                    member=scope, **control_grant()
                )

        async def inject_controls(loop: AgentLoopState, kind: str) -> bool:
            if detached_controls is None:
                return False
            assert (
                isinstance(scope, SubagentGraphScope)
                and control_grant is not None
            )
            node.current_grant()
            messages = detached_controls.claim_controls(
                kind, member=scope, **control_grant()
            )
            existing = {
                item.get("id")
                for item in loop.messages
                if isinstance(item.get("id"), str)
            }
            pending = [
                message for message in messages if message["id"] not in existing
            ]
            if not pending:
                return False
            loop.messages.extend(pending)
            loop.completed = False
            await save(loop, loop.pending_calls)
            return True

        async def dispatch(calls: list[ModelToolCall], loop: AgentLoopState):
            return await node.dispatch(
                calls, loop, authorize_effects=authorize_batch
            )

        if detached_controls is not None:
            assert (
                isinstance(scope, SubagentGraphScope)
                and control_grant is not None
            )
            detached_controls.acknowledge_controls(
                member=scope, **control_grant()
            )
            if state.completed and not state.graph_commands:
                await inject_controls(state, "turn")
        if not state.pending_calls and reconcile(state):
            await save(state, [])
        await run_agent_loop(
            state,
            stream_tools=stream_tools,
            stream_text=stream_text,
            dispatch=dispatch,
            on_text=text,
            on_thought=thought,
            save_state=save,
            before_model=node.refresh_tools,
            validate_model_input=node.validate_model_input,
            preflight_calls=node.preflight,
            on_stream_chunk=stream_boundary,
            on_boundary=boundary,
            inject_controls=inject_controls
            if detached_controls is not None
            else None,
        )
        node.current_grant()
        return GraphMemberResult(state.messages, state.graph_commands)

    check()
    heartbeat = (
        asyncio.create_task(lease.keep_alive()) if team_scope is None else None
    )
    try:
        return await run_multi_agent_graph(
            lease,
            root_agent_id,
            history,
            execute_member=execute,
            check_grant=check,
            recursion_limit=recursion_limit,
            result_only=result_only,
            human_actions=team_scope is None,
            public_output=publish_output,
            team_scope=team_scope,
            team_root=team_root,
        )
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
