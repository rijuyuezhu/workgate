"""Authenticated execution of one admitted native generation and its children."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Collection
from contextlib import suppress

from ..hosted_chat.agent_runtime import (
    AgentContext,
    AgentLoopState,
    AgentSuspended,
)
from ..hosted_chat.database import SQLiteDatabase
from ..hosted_chat.durable_tasks import DurableTaskRepository
from ..hosted_chat.generation_actor_lease import ActorLease, ActorLeaseLost
from ..hosted_chat.generation_context import GenerationContextResolver
from ..hosted_chat.generations import GenerationStore
from ..hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from ..hosted_chat.machine_permissions import effective_machine_access
from ..hosted_chat.memory_application import apply_memory_proposals
from ..hosted_chat.memory_extraction import extract_memory_proposals
from ..hosted_chat.provider_common import ModelToolCall
from ..hosted_chat.providers import stream_model, stream_model_with_tools
from ..hosted_chat.steering import SteeringStore
from ..hosted_chat.storage import WorkspaceRepository
from ..hosted_chat.subagents import build_subagent_configs, chat_subagent_config
from .hosted_chat_child_host import AuthenticatedChildHost
from .hosted_chat_compaction import ForegroundCompaction
from .hosted_chat_foreground_loop import (
    ObservableStreamInterrupted,
    run_foreground_native_loop,
    safe_generation_error,
)
from .hosted_chat_graph_loop import run_native_graph
from .hosted_chat_native_creation import _grant, _registered
from .hosted_chat_native_tool_node import NativeLoopToolNode
from .hosted_chat_root_delegation import RootDelegation
from .runtime import ControlRuntime


async def run_authenticated_generation(
    runtime: ControlRuntime,
    database: SQLiteDatabase,
    owner: str,
    run_id: str,
    *,
    scopes: set[str],
    expires: int | None,
    allowed_tools: Collection[str],
    task_machine: str | None,
    check_grant: Callable[[], None],
    authorize_batch: Callable[
        [list[ModelToolCall], AgentLoopState], Awaitable[None]
    ],
    allow_account: bool = False,
    stream_tools=stream_model_with_tools,
    stream_text=stream_model,
) -> bool:
    """Execute one owned root and drain its eligible children under a live grant.

    The caller owns the coroutine, not a request-detached global scheduler.
    Suspension or shutdown releases leases; the next authenticated admission
    can recover the existing durable work.
    """
    if database.root != runtime.config.state_dir / "hosted-chat":
        raise PermissionError(
            "Generation host belongs to another Control runtime"
        )
    scopes = set(scopes)
    allowed_tools = frozenset(allowed_tools)

    def live_grant() -> tuple[set[str], int | None]:
        check_grant()
        live, deadline = _grant(runtime, owner, scopes, expires)
        if not scopes.issubset(live) or (
            deadline is not None and deadline <= time.time()
        ):
            raise PermissionError(
                "Generation authentication grant expired or reduced"
            )
        return live, deadline

    live_grant()
    lease = ActorLease.claim(database, owner, run_id)
    if lease is None:
        return False
    store = GenerationStore(database)
    resolver = GenerationContextResolver(
        database, owner, allow_account=allow_account
    )
    workspace = WorkspaceRepository(database)
    tasks: list[asyncio.Task] = []
    try:
        generation = store.get(owner, run_id)
        if generation is None:
            raise ActorLeaseLost("Generation no longer available")
        context = await resolver.root(generation)
        root_agent = context.agent
        is_graph = root_agent is not None and bool(
            root_agent.get("edges") or root_agent.get("graphAgentIds")
        )
        history = resolver.history(generation, context)

        def check_root() -> None:
            live_grant()
            resolver.root_agent(generation)

        with database.read_snapshot() as db:
            manual = (
                json.loads(
                    db.execute(
                        "SELECT state FROM runs WHERE id=?", (run_id,)
                    ).fetchone()[0]
                ).get("manualCompaction")
                is True
            )
        if manual:
            node = NativeLoopToolNode(
                runtime,
                DurableTaskRepository(database),
                OwnedLoopCheckpoint(lease),
                allowed_tools=set(),
                scopes=scopes,
                expires=expires,
                task_machine=None,
                check_grant=check_root,
            )
            compaction = ForegroundCompaction(
                node,
                stream_text,
                preempt=lambda: SteeringStore(database).has_preempt(
                    owner, run_id
                ),
            )
            await compaction.manual(AgentLoopState(context, history))
            check_root()
            store.finish(owner, run_id, status="complete", actor_lease=lease)
            return True

        def current_access() -> dict:
            live, deadline = live_grant()
            if (
                workspace.conversation(owner, generation.conversation_id)
                is None
            ):
                raise PermissionError(
                    "Generation conversation is no longer owned"
                )
            agent = (
                None
                if root_agent is None
                else resolver.agents.get(owner, root_agent["id"])
            )
            if agent != root_agent:
                raise PermissionError("Root Agent revision changed")
            policy, _ = effective_machine_access(
                owner,
                generation.conversation_id,
                agent=agent,
                ancestors=(),
                scopes=live,
                expires=deadline,
                repo=workspace,
                registered=_registered(runtime),
            )
            return {
                "scopes": sorted(live),
                "machines": {
                    machine: level
                    for machine, level in policy.items()
                    if level != "none"
                },
                "task_machine": task_machine,
                "allowed_tools": sorted(allowed_tools),
            }

        host = AuthenticatedChildHost(
            runtime,
            database,
            owner,
            generation.conversation_id,
            scopes=scopes,
            expires=expires,
            allowed_tools=allowed_tools,
            check_grant=check_grant,
            current_access=current_access,
            resolve_child=resolver.child,
            resolve_wakeup=resolver.root,
            authorize_batch=authorize_batch,
            stream_tools=stream_tools,
            stream_text=stream_text,
        )

        async def resolve_child(agent: dict) -> AgentContext:
            return await resolver.child(generation, agent)

        async def automatic_memory() -> None:
            # Run before finishing the owned root; never create a separate
            # unattended actor. Isolate an optional memory model failure.
            try:
                check_root()
                if not lease.renew():
                    raise ActorLeaseLost("Memory root lease lost")
                proposals = await extract_memory_proposals(
                    lease, check_grant=check_root, stream_tools=stream_tools
                )
                if proposals is not None:
                    _, deadline = live_grant()
                    apply_memory_proposals(
                        lease,
                        proposals,
                        expires=deadline,
                        check_grant=check_root,
                    )
            except Exception as error:
                logging.getLogger(__name__).warning(
                    "Automatic memory skipped (%s)", type(error).__name__
                )

        async def graph_turn() -> None:
            assert root_agent is not None
            model_steps = await run_native_graph(
                runtime,
                lease,
                root_agent["id"],
                history,
                resolve_context=resolve_child,
                authorize_batch=authorize_batch,
                allowed_tools=allowed_tools,
                scopes=scopes,
                expires=expires,
                task_machine=task_machine,
                check_grant=check_root,
                recursion_limit=min(
                    context.recursion_limit or 2**31 - 1, 2**31 - 1
                ),
                stream_tools=stream_tools,
                stream_text=stream_text,
                publish_output=True,
            )
            await automatic_memory()
            check_root()
            store.finish(
                owner,
                run_id,
                status="complete",
                actor_lease=lease,
                model_steps=model_steps or None,
            )

        child_task = asyncio.create_task(host.serve())
        tasks.append(child_task)
        if is_graph:
            foreground = asyncio.create_task(graph_turn())
        else:
            configs = (
                [chat_subagent_config(owner)]
                if root_agent is None
                else build_subagent_configs(owner, root_agent, database)
            )
            delegation = RootDelegation(
                configs,
                resolve_child,
                on_reserved=host.on_reserved,
                stream_tools=stream_tools,
                stream_text=stream_text,
            )
            node = NativeLoopToolNode(
                runtime,
                DurableTaskRepository(database),
                OwnedLoopCheckpoint(lease),
                allowed_tools=allowed_tools,
                scopes=scopes,
                expires=expires,
                task_machine=task_machine,
                check_grant=check_root,
                delegation=delegation,
            )
            foreground = asyncio.create_task(
                run_foreground_native_loop(
                    node,
                    context,
                    history,
                    authorize_batch=authorize_batch,
                    before_finish=automatic_memory,
                    stream_tools=stream_tools,
                    stream_text=stream_text,
                )
            )
        tasks.append(foreground)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if child_task in done and foreground not in done:
            await child_task
            raise PermissionError("Authenticated child host lost authority")
        await foreground
        lease.release()
        host.finish_when_idle()
        await child_task
        return True
    except AgentSuspended:
        return True
    except ActorLeaseLost:
        return False
    except asyncio.CancelledError:
        raise
    except Exception as error:
        with suppress(ActorLeaseLost):
            store.finish(
                owner,
                run_id,
                status="interrupted"
                if isinstance(error, ObservableStreamInterrupted)
                else "error",
                error=safe_generation_error(error),
                actor_lease=lease,
            )
        raise
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        lease.release()
