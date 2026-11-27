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
from ..hosted_chat.durable_tasks import DurableTaskRepository
from ..hosted_chat.generation_actor_lease import ActorLease
from ..hosted_chat.graph_runtime import (
    GraphActivation,
    GraphMemberResult,
    run_multi_agent_graph,
)
from ..hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from ..hosted_chat.provider_common import ModelToolCall
from ..hosted_chat.providers import stream_model, stream_model_with_tools
from ..hosted_chat.steering import SteeringStore, reconcile_applied_steers
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
    ],
    allowed_tools: Collection[str],
    scopes: set[str],
    expires: int | None,
    task_machine: str | None,
    check_grant: Callable[[], None],
    recursion_limit: int,
    stream_tools=stream_model_with_tools,
    stream_text=stream_model,
    result_only: bool = False,
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
    steering = SteeringStore(lease.database)
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
        recovered = checkpoint.restore(context)
        state = (
            recovered.state
            if recovered is not None
            else AgentLoopState(context, copy.deepcopy(activation.history))
        )

        async def discard_delta(_value: str) -> None:
            node.current_grant()

        async def stream_boundary() -> bool:
            node.current_grant()
            return steering.has_preempt(lease.owner, lease.run_id)

        def reconcile(loop: AgentLoopState) -> bool:
            added = reconcile_applied_steers(
                loop.messages,
                steering.applied_parts(lease, scope=activation.scope),
            )
            if added:
                loop.completed = False
            return added

        async def boundary(loop: AgentLoopState, kind: str) -> bool:
            node.current_grant()
            steering.drain(lease, boundary=kind, scope=activation.scope)
            return reconcile(loop)

        async def save(
            loop: AgentLoopState, calls: list[ModelToolCall]
        ) -> None:
            node.current_grant()
            await checkpoint.save(loop, calls)

        async def dispatch(calls: list[ModelToolCall], loop: AgentLoopState):
            return await node.dispatch(
                calls, loop, authorize_effects=authorize_batch
            )

        if not state.pending_calls and reconcile(state):
            await save(state, [])
        await run_agent_loop(
            state,
            stream_tools=stream_tools,
            stream_text=stream_text,
            dispatch=dispatch,
            on_text=discard_delta,
            on_thought=discard_delta,
            save_state=save,
            before_model=node.refresh_tools,
            preflight_calls=node.preflight,
            on_stream_chunk=stream_boundary,
            on_boundary=boundary,
        )
        node.current_grant()
        return GraphMemberResult(state.messages, state.graph_commands)

    check()
    heartbeat = asyncio.create_task(lease.keep_alive())
    try:
        return await run_multi_agent_graph(
            lease,
            root_agent_id,
            history,
            execute_member=execute,
            check_grant=check,
            recursion_limit=recursion_limit,
            result_only=result_only,
            human_actions=True,
        )
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
