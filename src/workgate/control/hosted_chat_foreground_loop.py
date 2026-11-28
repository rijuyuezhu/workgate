"""Private foreground generation driver over the owned native Agent loop."""

import asyncio
import copy
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress

import httpx

from ..hosted_chat.agent_runtime import (
    AgentContext,
    AgentLoopState,
    AgentSuspended,
    RuntimeSafetyError,
    ToolBatchOutput,
    run_agent_loop,
)
from ..hosted_chat.context_usage import record_root_context_usage
from ..hosted_chat.generation_actor_lease import ActorLeaseLost
from ..hosted_chat.generations import GenerationStore
from ..hosted_chat.provider_common import (
    ModelFinish,
    ModelThought,
    ModelToolCall,
    ModelUsage,
)
from ..hosted_chat.providers import stream_model, stream_model_with_tools
from ..hosted_chat.steering import SteeringStore, reconcile_applied_steers
from .hosted_chat_compaction import ForegroundCompaction
from .hosted_chat_native_tool_node import NativeLoopToolNode


class ObservableStreamInterrupted(RuntimeSafetyError):
    """A crash occurred after public output but before its durable loop boundary."""


def safe_generation_error(error: Exception) -> str:
    """Return provider failure context without leaking request URLs or tokens."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"Model provider returned HTTP {error.response.status_code} (HTTPStatusError)"
    if isinstance(error, httpx.TransportError):
        return f"Model provider stream disconnected ({type(error).__name__})"
    if isinstance(error, ObservableStreamInterrupted):
        return "Provider stream was interrupted after observable output; cannot replay it safely"
    return f"Generation failed ({type(error).__name__})"


async def run_foreground_native_loop(
    node: NativeLoopToolNode,
    context: AgentContext,
    history: list[dict],
    *,
    authorize_batch: Callable[
        [list[ModelToolCall], AgentLoopState], Awaitable[None]
    ],
    before_finish: Callable[[], Awaitable[None]] | None = None,
    stream_tools: Callable[
        ...,
        AsyncIterator[
            str | ModelToolCall | ModelThought | ModelUsage | ModelFinish
        ],
    ] = stream_model_with_tools,
    stream_text: Callable[
        ..., AsyncIterator[str | ModelThought | ModelUsage | ModelFinish]
    ] = stream_model,
) -> AgentLoopState:
    """Run a caller-authorized native turn; no public route or grant admission.

    Content-policy authorization must be supplied by the caller. Native
    approval mode and immutable review claims are applied in the tool node.
    A caller owns lease admission/release; the running coroutine renews it.
    """
    checkpoint = node.checkpoint
    lease = checkpoint.lease
    store = GenerationStore(lease.database)
    steering = SteeringStore(lease.database)
    node.current_grant()
    generation = store.get(lease.owner, lease.run_id)
    if generation is None:
        raise ActorLeaseLost("Generation is no longer available")
    heartbeat = (
        asyncio.create_task(lease.keep_alive())
        if lease.wakeup_task_id is None
        else None
    )
    partial_text: list[str] = []
    cursor = 0

    async def emit(value: str, kind: str) -> None:
        nonlocal cursor
        node.current_grant()
        if not value:
            return
        part = {"type": kind, kind: value}
        if kind == "text":
            part["phase"] = "final_answer"
            partial_text.append(value)
        cursor = store.append(
            lease.owner,
            lease.run_id,
            {
                "event": "on_message_delta"
                if kind == "text"
                else "on_reasoning_delta",
                "data": {
                    "id": generation.response_message_id,
                    "delta": {"content": part},
                },
            },
            actor_lease=lease,
        )

    async def text(value: str) -> None:
        await emit(value, "text")

    async def thought(value: str) -> None:
        await emit(value, "think")

    async def save(state: AgentLoopState, calls: list[ModelToolCall]) -> None:
        node.current_grant()
        await checkpoint.save(
            state,
            calls,
            partial_text="".join(partial_text),
            public_delta_cursor=cursor,
        )

    async def dispatch(
        calls: list[ModelToolCall], state: AgentLoopState
    ) -> ToolBatchOutput:
        node.current_grant()
        return await node.dispatch(
            calls, state, authorize_effects=authorize_batch
        )

    def reconcile_steers(state: AgentLoopState) -> bool:
        added = reconcile_applied_steers(
            state.messages, steering.applied_parts(lease)
        )
        if added:
            state.completed = False
        return added

    async def boundary(state: AgentLoopState, kind: str) -> bool:
        node.current_grant()
        # Seal observable model output before publishing steer parts. At the
        # post-tool edge the original pending batch and its receipts already
        # provide recovery evidence; never save an inconsistent pending batch.
        if kind in {"stream", "terminal"}:
            if not steering.pending(lease.owner, lease.run_id):
                return reconcile_steers(state)
            await save(state, [])
        steering.drain(lease, boundary=kind)
        return reconcile_steers(state)

    async def on_model_usage(usage: ModelUsage, state: AgentLoopState) -> None:
        node.current_grant()
        try:
            record_root_context_usage(lease, state, usage)
        except ActorLeaseLost:
            raise
        except Exception as error:
            # Usage accounting is observational, not a generation effect.
            logging.getLogger(__name__).warning(
                "Provider context usage unavailable (%s)", type(error).__name__
            )

    async def check_stream() -> bool:
        node.current_grant()
        return steering.has_preempt(lease.owner, lease.run_id)

    compaction = ForegroundCompaction(
        node,
        stream_text,
        preempt=lambda: steering.has_preempt(lease.owner, lease.run_id),
    )
    try:
        restored = checkpoint.restore(context)
        if restored is None:
            state = AgentLoopState(context, copy.deepcopy(history))
        else:
            state = restored.state
            partial_text.append(restored.partial_text)
            cursor = restored.public_delta_cursor
        if store.public_delta_cursor(lease) != cursor:
            raise ObservableStreamInterrupted(
                "Uncheckpointed public provider delta"
            )
        if not state.pending_calls and reconcile_steers(state):
            await save(state, [])
        await run_agent_loop(
            state,
            stream_tools=stream_tools,
            stream_text=stream_text,
            dispatch=dispatch,
            on_text=text,
            on_thought=thought,
            save_state=save,
            before_model=compaction.before_model,
            on_model_usage=on_model_usage,
            validate_model_input=node.validate_model_input,
            project_messages=compaction.project,
            preflight_calls=node.preflight,
            on_stream_chunk=check_stream,
            on_boundary=boundary,
        )
        node.current_grant()
        if before_finish is not None:
            await before_finish()
        node.current_grant()
        store.finish(
            lease.owner, lease.run_id, status="complete", actor_lease=lease
        )
        return state
    except ActorLeaseLost, AgentSuspended, asyncio.CancelledError:
        raise
    except Exception as error:
        store.finish(
            lease.owner,
            lease.run_id,
            status="interrupted"
            if isinstance(error, ObservableStreamInterrupted)
            else "error",
            error=safe_generation_error(error),
            actor_lease=lease,
        )
        raise
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
