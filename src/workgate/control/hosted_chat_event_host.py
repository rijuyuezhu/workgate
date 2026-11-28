"""Supervised external event actors, with source-owned model-only leases."""

import asyncio

from ..hosted_chat.database import SQLiteDatabase
from ..hosted_chat.event_generation_queue import (
    EventGeneration,
    EventGenerations,
)
from ..hosted_chat.generation_actor_lease import ActorLease
from ..hosted_chat.generation_supervisor import GenerationCapacityExceeded
from ..hosted_chat.providers import stream_model
from .hosted_chat_schedule_host import run_model_only_generation
from .runtime import ControlRuntime


async def run_event_generation(
    runtime: ControlRuntime,
    database: SQLiteDatabase,
    item: EventGeneration,
    *,
    stream_text=stream_model,
) -> bool:
    if database.root != runtime.config.state_dir / "hosted-chat":
        raise PermissionError("Event generation belongs to another Control")
    if not runtime.config.ui_enabled or not runtime.config.hosted_chat_enabled:
        return False
    lease = ActorLease.claim(
        database, item.owner, item.run_id, event_delivery_id=item.delivery_id
    )
    if lease is None:
        return False
    return await run_model_only_generation(
        runtime, lease, stream_text=stream_text
    )


async def tick_event_generations(
    runtime: ControlRuntime, database: SQLiteDatabase
) -> None:
    if not runtime.config.ui_enabled or not runtime.config.hosted_chat_enabled:
        return
    queue = EventGenerations(database)
    await asyncio.to_thread(queue.refresh)
    for item in await asyncio.to_thread(queue.admit):

        async def execute(current=item):
            try:
                return await run_event_generation(runtime, database, current)
            finally:
                await asyncio.to_thread(queue.refresh)

        try:
            runtime.generation_supervisor.start(
                item.owner, item.run_id, execute
            )
        except GenerationCapacityExceeded:
            continue
