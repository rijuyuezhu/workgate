"""Recover exact settled sibling results at the owned model-loop tool edge."""

import copy
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from .agent_execution import (
    execute_tool_batch,
    replay_key,
    validate_settled_proposal,
)
from .loop_checkpoint import OwnedLoopCheckpoint, _Snapshot
from .provider_common import ModelToolCall, complete_tool_batch

_RECEIPT_CHANNEL = "tool_receipt"


async def execute_checkpointed_batch(
    checkpoint: OwnedLoopCheckpoint,
    calls: list[ModelToolCall],
    execute: Callable[[ModelToolCall], Awaitable[Any]],
    *,
    check_grant: Callable[[], None],
    preflight_unsettled: Callable[[list[ModelToolCall]], Awaitable[None]]
    | None = None,
    interrupting_names: frozenset[str] = frozenset(),
    is_interrupt: Callable[[BaseException], bool] = lambda error: False,
) -> list[Any]:
    """Settle the saved batch, persisting exact receipts before releasing results.

    Unsettled effects must still use the native intent ledger. A receipt is
    evidence of completion, not an authorization to perform or repeat work.
    The caller must check the current grant even when every result is cached.
    """

    def check() -> None:
        checkpoint.assert_current()
        check_grant()

    check()
    calls = copy.deepcopy(complete_tool_batch(calls))
    config = copy.deepcopy(checkpoint.active)
    row = checkpoint.saver.get_tuple(checkpoint.base)
    if (
        row is None
        or row["config"] != config
        or row["metadata"].get("source") != "loop"
    ):
        raise ValueError("Missing owned tool batch checkpoint")
    snapshot = _Snapshot.model_validate(row["checkpoint"]["channel_values"])
    saved = snapshot.calls()
    if not snapshot.tool_step_started or json.dumps(
        [asdict(call) for call in saved], sort_keys=True, allow_nan=False
    ) != json.dumps(
        [asdict(call) for call in calls], sort_keys=True, allow_nan=False
    ):
        raise ValueError("Tool batch does not match the saved started edge")
    by_id = {
        f"receipt:{call.call_id}": (i, call) for i, call in enumerate(calls)
    }
    settled = {}
    for task_id, channel, record in row["pendingWrites"]:
        if channel != _RECEIPT_CHANNEL:
            if task_id in by_id:
                raise ValueError("Unexpected write in a reserved receipt slot")
            continue
        if task_id not in by_id:
            raise ValueError("Unknown settled tool receipt")
        position, call = by_id[task_id]
        validate_settled_proposal(call, record)
        key = replay_key(call, position)
        if key in settled:
            raise ValueError("Duplicate settled tool receipt")
        settled[key] = record

    if preflight_unsettled is not None:
        await preflight_unsettled(
            [
                copy.deepcopy(call)
                for position, call in enumerate(calls)
                if replay_key(call, position) not in settled
            ]
        )

    async def run(call: ModelToolCall) -> Any:
        check()
        return await execute(call)

    async def complete(call: ModelToolCall, record: dict) -> None:
        check()
        checkpoint.saver.put_writes(
            config,
            [(_RECEIPT_CHANNEL, record)],
            task_id=f"receipt:{call.call_id}",
        )

    return await execute_tool_batch(
        calls,
        run,
        settled=settled,
        check_breaker=check,
        interrupting_names=interrupting_names,
        is_interrupt=is_interrupt,
        on_completed=complete,
    )
