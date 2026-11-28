"""Control-owned model-only scheduled occurrences, without a native grant."""

import asyncio
from contextlib import aclosing, suppress

from ..hosted_chat.agent_resources import (
    agent_context_messages,
    with_agent_context,
)
from ..hosted_chat.content_filters import (
    filters_from_snapshot,
    inspect_content,
    model_message_fragments,
    validate_agent_instructions,
    validate_model_parameters,
)
from ..hosted_chat.database import SQLiteDatabase
from ..hosted_chat.generation_actor_lease import ActorLease, ActorLeaseLost
from ..hosted_chat.generation_context import GenerationContextResolver
from ..hosted_chat.generation_supervisor import GenerationCapacityExceeded
from ..hosted_chat.generations import GenerationStore
from ..hosted_chat.message_media import project_message_media
from ..hosted_chat.provider_common import ModelFinish, ModelThought, ModelUsage
from ..hosted_chat.providers import stream_model
from ..hosted_chat.sampling import validate_sampling
from ..hosted_chat.schedule_occurrences import (
    ScheduleOccurrence,
    ScheduleOccurrences,
)
from .hosted_chat_foreground_loop import safe_generation_error
from .runtime import ControlRuntime

_MAX_OUTPUT_BYTES = 1024 * 1024
_MAX_OUTPUT_TOKENS = 16384
_RUN_TIMEOUT = 300


async def run_scheduled_generation(
    runtime: ControlRuntime,
    database: SQLiteDatabase,
    occurrence: ScheduleOccurrence,
    *,
    stream_text=stream_model,
) -> bool:
    if database.root != runtime.config.state_dir / "hosted-chat":
        raise PermissionError("Scheduled generation belongs to another Control")
    if not runtime.config.ui_enabled or not runtime.config.hosted_chat_enabled:
        return False
    owner, run_id = occurrence.owner, occurrence.run_id
    lease = ActorLease.claim(
        database, owner, run_id, schedule_occurrence_id=occurrence.id
    )
    if lease is None:
        return False
    return await run_model_only_generation(
        runtime,
        lease,
        conversation_id=occurrence.conversation_id,
        allow_files=True,
        stream_text=stream_text,
    )


async def run_model_only_generation(
    runtime: ControlRuntime,
    lease: ActorLease,
    *,
    conversation_id: str | None = None,
    allow_files: bool = False,
    stream_text=stream_model,
) -> bool:
    """Run a server-admitted actor without an interactive authorization grant."""
    database, owner, run_id = lease.database, lease.owner, lease.run_id
    store = GenerationStore(database)
    heartbeat = asyncio.create_task(lease.keep_alive())
    try:
        generation = store.get(owner, run_id)
        if generation is None or (
            conversation_id is not None
            and generation.conversation_id != conversation_id
        ):
            raise PermissionError("Unattended generation identity changed")
        resolver = GenerationContextResolver(
            database, owner, allow_account=False
        )
        context = await resolver.root(generation)
        agent = context.agent
        if agent is None or agent.get("edges") or agent.get("graphAgentIds"):
            raise PermissionError(
                "Unattended execution requires a single saved Agent"
            )
        history = resolver.history(generation, context)

        def check():
            lease.assert_current()
            if (
                not runtime.config.hosted_chat_enabled
                or not runtime.config.ui_enabled
            ):
                raise PermissionError("Hosted chat disabled")
            if resolver.root_agent(generation) != agent:
                raise PermissionError("Unattended Agent revision changed")

        check()
        with database.read_snapshot() as db:
            lease.assert_current(db)
            if allow_files:
                messages = project_message_media(
                    database,
                    db,
                    owner,
                    with_agent_context(
                        history,
                        agent_context_messages(database, db, owner, agent),
                    ),
                )
            else:
                # Source keys do not grant access to files or native context.
                messages = [
                    {"role": message["role"], "content": message["content"]}
                    for message in history
                    if message["role"] in {"system", "user", "assistant"}
                    and isinstance(message.get("content"), str)
                ]
            filters = filters_from_snapshot(db, owner)
        validate_agent_instructions(agent.get("instructions") or "", filters)
        validate_model_parameters(context.parameters, filters)
        blocked = inspect_content(model_message_fragments(messages), filters)
        if blocked is not None:
            raise blocked
        sampling = validate_sampling(
            {
                key: value
                for key, value in context.parameters.items()
                if key in {"temperature", "top_p", "max_tokens"}
                and value is not None
            },
            provider=context.profile.provider,
        )
        sampling["max_tokens"] = min(
            sampling.get("max_tokens", _MAX_OUTPUT_TOKENS), _MAX_OUTPUT_TOKENS
        )
        check()
        output_size = 0
        # No tool-bearing transport, execution context, native tool node or
        # account authorization. A scheduled occurrence never inherits caller
        # credentials or exposes a tool catalog.
        async with (
            asyncio.timeout(_RUN_TIMEOUT),
            aclosing(
                stream_text(context.profile, messages, **sampling)
            ) as events,
        ):
            async for item in events:
                check()
                if isinstance(item, str):
                    kind, text = "text", item
                elif isinstance(item, ModelThought):
                    kind, text = "think", item.text
                elif isinstance(item, ModelUsage | ModelFinish):
                    continue
                else:
                    raise PermissionError("Unexpected unattended model event")
                if not text:
                    continue
                output_size += len(text.encode("utf-8"))
                if output_size > _MAX_OUTPUT_BYTES:
                    raise ValueError("Unattended output exceeds limit")
                # Bounded chunks fit the generic event log envelope.
                for offset in range(0, len(text), 16384):
                    part = {"type": kind, kind: text[offset : offset + 16384]}
                    if kind == "text":
                        part["phase"] = "final_answer"
                    store.append(
                        owner,
                        run_id,
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
        check()
        store.finish(owner, run_id, status="complete", actor_lease=lease)
        return True
    except ActorLeaseLost:
        return False
    except asyncio.CancelledError:
        with suppress(ActorLeaseLost):
            store.finish(
                owner,
                run_id,
                status="interrupted",
                error="Unattended generation interrupted",
                actor_lease=lease,
            )
        raise
    except Exception as error:
        with suppress(ActorLeaseLost):
            store.finish(
                owner,
                run_id,
                status="error",
                error=safe_generation_error(error),
                actor_lease=lease,
            )
        return False
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        lease.release()


async def tick_scheduled_generations(
    runtime: ControlRuntime, database: SQLiteDatabase
) -> None:
    if not runtime.config.ui_enabled or not runtime.config.hosted_chat_enabled:
        return
    occurrences = ScheduleOccurrences(database)
    await asyncio.to_thread(occurrences.refresh)
    await asyncio.to_thread(occurrences.claim_due)
    for occurrence in await asyncio.to_thread(occurrences.pending):

        async def execute(item=occurrence):
            try:
                return await run_scheduled_generation(runtime, database, item)
            finally:
                await asyncio.to_thread(occurrences.refresh)

        try:
            runtime.generation_supervisor.start(
                occurrence.owner, occurrence.run_id, execute
            )
        except GenerationCapacityExceeded:
            # Leave the claim pending, not started; the next tick can dispatch.
            continue
