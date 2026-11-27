"""Direct tool-batch execution ported from agents 4.0.1 ToolNode.ts.

The source waits for every sibling, restores immutable completed proposals on
replay and runs interrupt-capable tools before ordinary side-effecting siblings.
Model routing/checkpoint channels are handled by the caller.
"""

import asyncio
import copy
import json
from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from typing import Any

from .provider_common import ModelToolCall, complete_tool_batch


def replay_key(call: ModelToolCall, position: int) -> str:
    return json.dumps(
        ["id", call.call_id] if call.call_id else ["position", position],
        separators=(",", ":"),
    )


def _same_args(left: Any, right: Any) -> bool:
    # JSON.stringify emits integral floats as integers and distinguishes
    # booleans from numbers; Python's ordinary equality does the opposite.
    def normalized(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: normalized(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalized(item) for item in value]
        if type(value) is float and value.is_integer():
            return int(value)
        return value

    return json.dumps(
        normalized(left), sort_keys=True, ensure_ascii=False, allow_nan=False
    ) == json.dumps(
        normalized(right),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )


def validate_settled_proposal(call: ModelToolCall, record: dict) -> None:
    """Require the exact original proposal and a complete settled output."""
    if not isinstance(record, dict) or set(record) != {"proposal", "output"}:
        raise ValueError("Invalid settled tool receipt")
    proposal = record["proposal"]
    if (
        not isinstance(proposal, dict)
        or set(proposal) != {"name", "args"}
        or proposal["name"] != call.name
        or not _same_args(proposal["args"], call.arguments)
    ):
        raise ValueError(
            "Cannot change an already completed tool call during approval replay"
        )


async def execute_tool_batch(
    calls: Sequence[ModelToolCall],
    execute: Callable[[ModelToolCall], Awaitable[Any]],
    *,
    settled: MutableMapping[str, dict] | None = None,
    interrupting_names: frozenset[str] = frozenset(),
    is_interrupt: Callable[[BaseException], bool] = lambda error: False,
    check_breaker: Callable[[], None] = lambda: None,
    on_completed: Callable[[ModelToolCall, dict], Awaitable[None]]
    | None = None,
) -> list[Any]:
    cache = settled if settled is not None else {}

    # Validate the entire batch before starting any side effects. A malformed
    # provider response may repeat a call ID, or alter a previously completed
    # proposal while other calls in the same batch are still pending.
    check_breaker()
    calls = copy.deepcopy(complete_tool_batch(list(calls)))
    keys = [replay_key(call, position) for position, call in enumerate(calls)]
    for key, call in zip(keys, calls, strict=True):
        cached = cache.get(key)
        if cached is not None:
            validate_settled_proposal(call, cached)

    async def run_one(position: int) -> Any:
        call = calls[position]
        key = keys[position]
        cached = cache.get(key)
        if cached is not None:
            return copy.deepcopy(cached["output"])
        proposal = {"name": call.name, "args": copy.deepcopy(call.arguments)}
        output = await execute(copy.deepcopy(call))
        result = {"proposal": proposal, "output": copy.deepcopy(output)}
        if on_completed is not None:
            await on_completed(copy.deepcopy(call), copy.deepcopy(result))
        cache[key] = result
        return output

    async def settle(
        positions: list[int], *, prefer_non_interrupt: bool
    ) -> list[Any]:
        results = await asyncio.gather(
            *(run_one(position) for position in positions),
            return_exceptions=True,
        )
        check_breaker()
        failures = [
            result for result in results if isinstance(result, BaseException)
        ]
        if failures:
            failure = (
                next(
                    (error for error in failures if not is_interrupt(error)),
                    failures[0],
                )
                if prefer_non_interrupt
                else failures[0]
            )
            raise failure
        return results

    interrupting = [
        i for i, call in enumerate(calls) if call.name in interrupting_names
    ]
    if not interrupting:
        return await settle(list(range(len(calls))), prefer_non_interrupt=True)
    ordinary = [
        i for i, call in enumerate(calls) if call.name not in interrupting_names
    ]
    outputs: list[Any] = [None] * len(calls)
    first = await settle(interrupting, prefer_non_interrupt=False)
    for position, output in zip(interrupting, first, strict=True):
        outputs[position] = output
    check_breaker()
    rest = await settle(ordinary, prefer_non_interrupt=True)
    for position, output in zip(ordinary, rest, strict=True):
        outputs[position] = output
    return outputs
