"""Private authenticated worker over the existing model and native tool loop."""

import asyncio
import copy
from collections.abc import Awaitable, Callable, Collection
from contextlib import suppress
from dataclasses import replace

from ..hosted_chat.agent_runtime import (
    AgentContext,
    AgentLoopState,
    RuntimeSafetyError,
    run_agent_loop,
)
from ..hosted_chat.database import SQLiteDatabase
from ..hosted_chat.detached_task_lease import DetachedTaskLease
from ..hosted_chat.durable_tasks import DurableTaskRepository
from ..hosted_chat.generation_actor_lease import ActorLeaseLost
from ..hosted_chat.generations import Generation
from ..hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from ..hosted_chat.provider_common import ModelToolCall
from ..hosted_chat.providers import stream_model, stream_model_with_tools
from ..hosted_chat.subagent_scope import DetachedExecutionScope
from .hosted_chat_native_creation import _grant
from .hosted_chat_native_tool_node import NativeLoopToolNode
from .runtime import ControlRuntime

_RENEW_SECONDS = 5
_TIMEOUT_SECONDS = 30 * 60


async def run_detached_subagent(
    runtime: ControlRuntime,
    database: SQLiteDatabase,
    owner: str,
    conversation: str,
    task_id: str,
    *,
    scopes: set[str],
    expires: int | None,
    disclosure_access: dict,
    allowed_tools: Collection[str],
    check_grant: Callable[[], None],
    resolve_context: Callable[
        [Generation, dict | None], Awaitable[AgentContext]
    ],
    authorize_batch: Callable[
        [list[ModelToolCall], AgentLoopState], Awaitable[None]
    ],
    stream_tools=stream_model_with_tools,
    stream_text=stream_model,
) -> bool:
    """Execute one reserved single-Agent child under a live authenticated caller.

    No process-global scheduler or unattended grant is implied. A replacement
    caller may recover the same child using the durable task lease/checkpoint.
    """
    if database.root != runtime.config.state_dir / "hosted-chat":
        raise PermissionError(
            "Detached worker belongs to another Control runtime"
        )
    scopes = set(scopes)
    disclosure_access = copy.deepcopy(disclosure_access)
    allowed_tools = frozenset(allowed_tools).intersection(
        disclosure_access.get("allowed_tools", ())
    )

    def grant() -> dict:
        check_grant()
        live, deadline = _grant(runtime, owner, scopes, expires)
        return {
            "scopes": live,
            "expires": deadline,
            "disclosure_access": disclosure_access,
        }

    lease = DetachedTaskLease.claim(
        database, owner, conversation, task_id, **grant()
    )
    if lease is None:
        return False

    def check() -> None:
        lease.assert_current(**grant())

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(_RENEW_SECONDS)
            lease.renew(**grant())

    async def execute() -> list[dict]:
        generation, actor = lease.materialize(**grant())
        scope = DetachedExecutionScope.from_actor(actor)
        if scope.team_definition_hash is not None:
            raise PermissionError("Detached team worker is not enabled")
        agent, history, recursion_limit = lease.execution_input(**grant())
        context = await resolve_context(generation, copy.deepcopy(agent))
        check()
        if (
            context.profile.model != generation.model
            or (context.profile.channel or context.profile.provider)
            != generation.provider
        ):
            raise PermissionError("Detached provider resolution changed")
        context = replace(
            context,
            agent=copy.deepcopy(agent),
            tools=[],
            parameters=copy.deepcopy(context.parameters),
            recursion_limit=min(
                (
                    limit
                    for limit in (context.recursion_limit, recursion_limit)
                    if limit is not None
                ),
                default=None,
            ),
            compaction_semantic_index=None,
            fading_tier=None,
        )
        checkpoint = OwnedLoopCheckpoint(actor, scope=scope)
        node = NativeLoopToolNode(
            runtime,
            DurableTaskRepository(database, scope=scope),
            checkpoint,
            allowed_tools=allowed_tools,
            scopes=set(scope.admission.scopes),
            expires=scope.admission.grant_expires,
            task_machine=disclosure_access.get("task_machine"),
            check_grant=check,
        )
        recovered = checkpoint.restore(context)
        if recovered is not None and (
            recovered.partial_text or recovered.public_delta_cursor
        ):
            raise RuntimeSafetyError(
                "Detached child checkpoint contains public output"
            )
        state = (
            recovered.state
            if recovered is not None
            else AgentLoopState(context, history)
        )

        async def discard(_value: str) -> None:
            node.current_grant()

        async def stream_boundary() -> bool:
            node.current_grant()
            return lease.has_interrupt(**grant())

        async def save(
            loop: AgentLoopState, calls: list[ModelToolCall]
        ) -> None:
            node.current_grant()
            await checkpoint.save(loop, calls)
            lease.acknowledge_controls(**grant())

        async def inject_controls(loop: AgentLoopState, boundary: str) -> bool:
            node.current_grant()
            messages = lease.claim_controls(boundary, **grant())
            existing = {
                item.get("id")
                for item in loop.messages
                if isinstance(item.get("id"), str)
            }
            pending = [item for item in messages if item["id"] not in existing]
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

        node.current_grant()
        lease.acknowledge_controls(**grant())
        if state.completed:
            await inject_controls(state, "turn")
        await run_agent_loop(
            state,
            stream_tools=stream_tools,
            stream_text=stream_text,
            dispatch=dispatch,
            on_text=discard,
            on_thought=discard,
            save_state=save,
            before_model=node.refresh_tools,
            preflight_calls=node.preflight,
            on_stream_chunk=stream_boundary,
            inject_controls=inject_controls,
        )
        node.current_grant()
        if not state.completed:
            raise RuntimeSafetyError("Detached loop did not complete")
        return state.messages

    try:
        worker = asyncio.create_task(execute())
        renewal = asyncio.create_task(heartbeat())
        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS):
                done, _ = await asyncio.wait(
                    {worker, renewal}, return_when=asyncio.FIRST_COMPLETED
                )
                if renewal in done:
                    await renewal
                messages = await worker
        finally:
            worker.cancel()
            renewal.cancel()
            await asyncio.gather(worker, renewal, return_exceptions=True)
        lease.complete(messages, **grant())
        return True
    except asyncio.CancelledError:
        # Cooperative shutdown is recoverable, unless an explicit cancel won.
        with suppress(ActorLeaseLost, PermissionError):
            lease.acknowledge_cancel()
        raise
    except ActorLeaseLost:
        # A stale worker cannot settle a replacement's task. A current worker
        # that observed an explicit cancel can settle it through fail().
        with suppress(ActorLeaseLost):
            lease.fail()
        return False
    except Exception:
        with suppress(ActorLeaseLost):
            lease.fail()
        # Provider exceptions can carry credentials; keep the durable error
        # generic and do not send raw exception text to a host/tool caller.
        return False
    finally:
        lease.release()
