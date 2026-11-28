"""Authenticated completion runner using the existing foreground native loop."""

import asyncio
import copy
from collections.abc import Awaitable, Callable, Collection
from contextlib import suppress
from dataclasses import replace

from ..hosted_chat.agent_runtime import AgentContext, AgentLoopState
from ..hosted_chat.database import SQLiteDatabase
from ..hosted_chat.durable_tasks import DurableTaskRepository
from ..hosted_chat.generation_actor_lease import ActorLeaseLost
from ..hosted_chat.generation_context import with_agent_instructions
from ..hosted_chat.generations import Generation
from ..hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from ..hosted_chat.provider_common import ModelToolCall
from ..hosted_chat.providers import stream_model, stream_model_with_tools
from ..hosted_chat.subagent_wakeup import WakeupLease, reconcile_wakeups
from .hosted_chat_foreground_loop import run_foreground_native_loop
from .hosted_chat_native_creation import _grant
from .hosted_chat_native_tool_node import NativeLoopToolNode
from .runtime import ControlRuntime

_RENEW_SECONDS = 20


async def run_authenticated_wakeup(
    runtime: ControlRuntime,
    database: SQLiteDatabase,
    owner: str,
    task_id: str,
    *,
    scopes: set[str],
    expires: int | None,
    disclosure_access: dict,
    allowed_tools: Collection[str],
    check_grant: Callable[[], None],
    resolve_context: Callable[[Generation], Awaitable[AgentContext]],
    authorize_batch: Callable[
        [list[ModelToolCall], AgentLoopState], Awaitable[None]
    ],
    stream_tools=stream_model_with_tools,
    stream_text=stream_model,
) -> bool:
    """Run an admitted completion under the caller's live principal.

    No startup recovery grant or independent model executor is introduced.
    The original parent disclosure ceiling is rechecked at every loop boundary.
    """
    if database.root != runtime.config.state_dir / "hosted-chat":
        raise PermissionError(
            "Completion worker belongs to another Control runtime"
        )
    scopes = set(scopes)
    access = copy.deepcopy(disclosure_access)
    allowed_tools = frozenset(allowed_tools).intersection(
        access.get("allowed_tools", ())
    )
    expected = {
        key: access[key] for key in ("scopes", "machines", "task_machine")
    }

    def grant() -> dict:
        check_grant()
        live, deadline = _grant(runtime, owner, scopes, expires)
        return {
            "scopes": live,
            "expires": deadline,
            "disclosure_access": access,
        }

    claimed = WakeupLease.claim(database, owner, task_id, **grant())
    if claimed is None:
        return False
    generation, lease = claimed

    node: NativeLoopToolNode | None = None

    def check() -> None:
        current = grant()
        lease.assert_current(**current)
        if (
            node is not None
            and node._effective_receipt_access(set(node.scopes), node.expires)
            != expected
        ):
            raise PermissionError("Completion disclosure authority changed")

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(_RENEW_SECONDS)
            check()
            lease.renew(**grant())

    async def execute():
        nonlocal node
        original_scopes, deadline, history = lease.execution_input(**grant())
        context = await resolve_context(generation)
        check()
        if (
            (context.agent or {}).get("id") != generation.agent_id
            or context.profile.model != generation.model
            or (context.profile.channel or context.profile.provider)
            != generation.provider
        ):
            raise PermissionError(
                "Completion provider or Agent identity changed"
            )
        context = replace(
            context, tools=[], compaction_semantic_index=None, fading_tier=None
        )
        checkpoint = OwnedLoopCheckpoint(lease.actor)
        node = NativeLoopToolNode(
            runtime,
            DurableTaskRepository(database),
            checkpoint,
            allowed_tools=allowed_tools,
            scopes=original_scopes,
            expires=deadline,
            task_machine=access.get("task_machine"),
            check_grant=check,
        )
        check()
        await run_foreground_native_loop(
            node,
            context,
            with_agent_instructions(history, context.agent),
            authorize_batch=authorize_batch,
            stream_tools=stream_tools,
            stream_text=stream_text,
        )

    try:
        worker = asyncio.create_task(execute())
        renewal = asyncio.create_task(heartbeat())
        try:
            done, _ = await asyncio.wait(
                {worker, renewal}, return_when=asyncio.FIRST_COMPLETED
            )
            if renewal in done:
                await renewal
            await worker
        finally:
            worker.cancel()
            renewal.cancel()
            await asyncio.gather(worker, renewal, return_exceptions=True)
        return True
    except asyncio.CancelledError:
        raise
    except ActorLeaseLost:
        return False
    except Exception as error:
        with suppress(ActorLeaseLost):
            lease.fail_dispatch(error)
        return False
    finally:
        lease.release()
        reconcile_wakeups(database, owner)
