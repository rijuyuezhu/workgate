"""Real SQLite model-loop recovery under a replacement actor."""

import json

import pytest

from workgate.hosted_chat.agent_runtime import (
    AgentContext,
    AgentLoopState,
    RuntimeSafetyError,
    ToolBatchOutput,
    run_agent_loop,
)
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import (
    ActorLease,
    ActorLeaseLost,
)
from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from workgate.hosted_chat.provider_common import ModelToolCall, ProviderConfig


@pytest.fixture
def lease(tmp_path):
    database = SQLiteDatabase(tmp_path)
    with database.connect() as db:
        db.execute("INSERT INTO conversations(id,owner) VALUES('chat','alice')")
        db.execute(
            "INSERT INTO runs(id,conversation_id,status) VALUES('run','chat','running')"
        )
    result = ActorLease.claim(database, "alice", "run")
    assert result is not None
    return result


def context(token="secret-not-in-checkpoint", agent="agent"):
    return AgentContext(
        ProviderConfig("openai", "model", "https://provider.invalid", token),
        [{"name": "old-definition-not-in-checkpoint"}],
        agent={"id": agent},
    )


@pytest.mark.asyncio
async def test_restore_started_tool_boundary_after_actor_replacement(
    lease, monkeypatch
):
    checkpoint = OwnedLoopCheckpoint(lease)
    assert checkpoint.restore(context()) is None
    call = ModelToolCall(
        "id", "read", {"path": "file"}, {"reasoning": "opaque"}
    )
    state = AgentLoopState(
        context(),
        [{"role": "user", "content": "prompt"}],
        step=2,
        model_turn=1,
        pending_calls=[call],
        tool_step_started=True,
        repeated_tool_batches=1,
        last_tool_batch="prior-result",
    )
    await checkpoint.save(state, [call], partial_text="visible prefix")
    first = checkpoint.active["configurable"]["checkpoint_id"]
    monkeypatch.setattr(
        "workgate.hosted_chat.loop_checkpoint.time.time_ns", lambda: 1
    )
    await checkpoint.save(state, [call], partial_text="visible prefix")
    second = checkpoint.active["configurable"]["checkpoint_id"]
    assert second > first
    stored = checkpoint.saver.get_tuple(checkpoint.active)
    assert stored is not None
    encoded = json.dumps(stored)
    assert "secret-not-in-checkpoint" not in encoded
    assert "old-definition-not-in-checkpoint" not in encoded
    assert stored["parentConfig"]["configurable"]["checkpoint_id"] == first

    lease.release()
    fresh = ActorLease.claim(lease.database, lease.owner, lease.run_id)
    assert fresh is not None
    with pytest.raises(ActorLeaseLost):
        checkpoint.restore(context())
    with pytest.raises(ActorLeaseLost):
        await checkpoint.save(state, [call])
    resumed = OwnedLoopCheckpoint(fresh)
    with pytest.raises(RuntimeSafetyError, match="Restore existing"):
        await resumed.save(state, [call])
    with pytest.raises(RuntimeSafetyError, match="Checkpoint Agent"):
        resumed.restore(context(agent="another"))
    current = context("fresh-secret")
    current.tools = []
    current.tool_end = True
    recovery = resumed.restore(current)
    assert recovery is not None
    assert recovery.partial_text == "visible prefix"
    assert recovery.state.context is current
    assert recovery.state.pending_calls == [call]
    assert recovery.state.step == 2 and recovery.state.model_turn == 1
    assert recovery.state.tool_step_started
    assert recovery.state.repeated_tool_batches == 1
    assert recovery.state.last_tool_batch == "prior-result"

    async def dispatch(calls, loop):
        assert calls == [call] and loop.step == 2
        return ToolBatchOutput(
            [{"role": "tool", "tool_call_id": "id", "content": "ok"}]
        )

    async def forbidden_stream(*args, **kwargs):
        raise AssertionError("started tool step must resume before the model")
        yield ""

    async def forbidden_text(*args):
        raise AssertionError("no text expected")

    await run_agent_loop(
        recovery.state,
        stream_tools=forbidden_stream,
        stream_text=forbidden_stream,
        dispatch=dispatch,
        on_text=forbidden_text,
        save_state=resumed.save,
    )
    terminal = OwnedLoopCheckpoint(fresh).restore(current)
    assert terminal is not None
    assert terminal.state.completed and not terminal.state.pending_calls
    assert terminal.state.step == 2
    assert terminal.state.messages[-1]["content"] == "ok"


@pytest.mark.asyncio
async def test_invalid_checkpoint_proposals_never_advance_the_head(lease):
    checkpoint = OwnedLoopCheckpoint(lease)
    state = AgentLoopState(context(), [])
    call = ModelToolCall("id", "read", {})
    with pytest.raises(ValueError, match="does not match"):
        await checkpoint.save(state, [call])
    state.pending_calls = [call, call]
    with pytest.raises(ValueError, match="duplicate"):
        await checkpoint.save(state, state.pending_calls)
    state.pending_calls = []
    state.tool_step_started = True
    with pytest.raises(ValueError, match="boundary"):
        await checkpoint.save(state, [])
    state.tool_step_started = False
    state.repeated_tool_batches = 1
    with pytest.raises(ValueError, match="progress"):
        await checkpoint.save(state, [])
    assert checkpoint.saver.get_tuple(checkpoint.base) is None


@pytest.mark.asyncio
async def test_settled_sibling_is_replayed_after_actor_replacement(lease):
    from workgate.hosted_chat.loop_receipts import execute_checkpointed_batch

    checkpoint = OwnedLoopCheckpoint(lease)
    calls = [
        ModelToolCall("one", "read", {"path": "first"}),
        ModelToolCall("two", "read", {"path": "second"}),
    ]
    state = AgentLoopState(
        context(),
        [],
        step=2,
        model_turn=1,
        pending_calls=calls,
        tool_step_started=True,
    )
    await checkpoint.save(state, calls)
    checkpoint.saver.put_writes(
        checkpoint.active, [("__resume__", "acknowledged")], "human"
    )
    effects = []

    async def interrupted(call):
        effects.append(call.call_id)
        if call.call_id == "two":
            raise RuntimeError("transport interrupted")
        return {"structuredContent": {"value": [1, 2]}, "content": []}

    with pytest.raises(RuntimeError, match="transport interrupted"):
        await execute_checkpointed_batch(
            checkpoint,
            calls,
            interrupted,
            check_grant=lambda: None,
        )
    assert sorted(effects) == ["one", "two"]
    lease.release()
    replacement = ActorLease.claim(lease.database, lease.owner, lease.run_id)
    assert replacement is not None
    recovered = OwnedLoopCheckpoint(replacement)
    assert recovered.restore(context()) is not None

    async def finish(call):
        effects.append(call.call_id)
        assert call.call_id == "two"
        return {"structuredContent": {"value": 3}}

    result = await execute_checkpointed_batch(
        recovered,
        calls,
        finish,
        check_grant=lambda: None,
    )
    assert result == [
        {"structuredContent": {"value": [1, 2]}, "content": []},
        {"structuredContent": {"value": 3}},
    ]
    assert effects.count("one") == 1 and effects.count("two") == 2
    result[0]["structuredContent"]["value"].append(99)
    assert (
        await execute_checkpointed_batch(
            recovered,
            calls,
            finish,
            check_grant=lambda: None,
        )
    )[0]["structuredContent"]["value"] == [1, 2]

    def revoked():
        raise PermissionError("grant revoked")

    with pytest.raises(PermissionError, match="revoked"):
        await execute_checkpointed_batch(
            recovered, calls, finish, check_grant=revoked
        )
    with pytest.raises(ActorLeaseLost):
        await execute_checkpointed_batch(
            checkpoint, calls, finish, check_grant=lambda: None
        )


@pytest.mark.asyncio
async def test_invalid_saved_receipts_reject_entire_batch_before_effect(lease):
    from workgate.hosted_chat.loop_receipts import execute_checkpointed_batch

    checkpoint = OwnedLoopCheckpoint(lease)
    calls = [
        ModelToolCall("one", "read", {"value": True}),
        ModelToolCall("two", "read", {}),
    ]
    state = AgentLoopState(
        context(),
        [],
        step=2,
        model_turn=1,
        pending_calls=calls,
        tool_step_started=True,
    )

    async def forbidden(call):
        raise AssertionError("no side effects before validating all receipts")

    await checkpoint.save(state, calls)
    with pytest.raises(ValueError, match="saved started edge"):
        await execute_checkpointed_batch(
            checkpoint,
            [ModelToolCall("one", "read", {"value": 1}), calls[1]],
            forbidden,
            check_grant=lambda: None,
        )
    checkpoint.saver.put_writes(
        checkpoint.active,
        [
            (
                "tool_receipt",
                {
                    "proposal": {"name": "read", "args": {"value": 1}},
                    "output": "bad",
                },
            ),
        ],
        "receipt:one",
    )
    with pytest.raises(ValueError, match="already completed"):
        await execute_checkpointed_batch(
            checkpoint, calls, forbidden, check_grant=lambda: None
        )

    await checkpoint.save(state, calls)
    checkpoint.saver.put_writes(
        checkpoint.active,
        [
            (
                "tool_receipt",
                {"proposal": {"name": "read", "args": {}}, "output": "bad"},
            ),
        ],
        "receipt:unknown",
    )
    with pytest.raises(ValueError, match="Unknown settled"):
        await execute_checkpointed_batch(
            checkpoint, calls, forbidden, check_grant=lambda: None
        )


@pytest.mark.asyncio
async def test_checkpoint_receipt_boundary_and_interrupt_order(lease):
    from workgate.hosted_chat.agent_runtime import AgentSuspended
    from workgate.hosted_chat.loop_receipts import execute_checkpointed_batch

    checkpoint = OwnedLoopCheckpoint(lease)
    calls = [
        ModelToolCall("ordinary", "read", {}),
        ModelToolCall("interrupt", "ask", {}),
    ]
    state = AgentLoopState(
        context(), [], step=1, model_turn=1, pending_calls=calls
    )
    seen = []

    async def execute(call):
        seen.append(call.call_id)
        if call.name == "ask":
            raise AgentSuspended("waiting")
        return "done"

    async def batch():
        return await execute_checkpointed_batch(
            checkpoint,
            calls,
            execute,
            check_grant=lambda: None,
            interrupting_names=frozenset({"ask"}),
            is_interrupt=lambda error: isinstance(error, AgentSuspended),
        )

    with pytest.raises(ValueError, match="Missing owned"):
        await batch()
    await checkpoint.save(state, calls)
    with pytest.raises(ValueError, match="saved started edge"):
        await batch()
    state.tool_step_started = True
    await checkpoint.save(state, calls)
    with pytest.raises(AgentSuspended):
        await batch()
    assert seen == ["interrupt"]
    checkpoint.saver.put_writes(
        checkpoint.active, [("other", "bad")], "receipt:ordinary"
    )
    with pytest.raises(ValueError, match="reserved receipt slot"):
        await batch()
    assert seen == ["interrupt"]


@pytest.mark.parametrize(
    "records,error",
    [
        ([{"proposal": {"name": "read", "args": {}}}], "Invalid settled"),
        (
            [
                {"proposal": {"name": "read", "args": {}}, "output": "one"},
                {"proposal": {"name": "read", "args": {}}, "output": "two"},
            ],
            "Duplicate settled",
        ),
    ],
)
@pytest.mark.asyncio
async def test_corrupt_receipt_slots_fail_closed(lease, records, error):
    from workgate.hosted_chat.loop_receipts import execute_checkpointed_batch

    checkpoint = OwnedLoopCheckpoint(lease)
    calls = [ModelToolCall("one", "read", {})]
    state = AgentLoopState(
        context(),
        [],
        step=2,
        model_turn=1,
        pending_calls=calls,
        tool_step_started=True,
    )
    await checkpoint.save(state, calls)
    checkpoint.saver.put_writes(
        checkpoint.active,
        [("tool_receipt", record) for record in records],
        "receipt:one",
    )

    async def forbidden(call):
        raise AssertionError("corrupt receipt must not dispatch")

    with pytest.raises(ValueError, match=error):
        await execute_checkpointed_batch(
            checkpoint, calls, forbidden, check_grant=lambda: None
        )


@pytest.mark.asyncio
async def test_pruned_checkpoint_never_restarts_an_owned_generation(lease):
    checkpoint = OwnedLoopCheckpoint(lease)
    state = AgentLoopState(context(), [])
    await checkpoint.save(state, [])
    checkpoint.saver.delete_generation(checkpoint.base)
    with pytest.raises(RuntimeSafetyError, match="no longer available"):
        checkpoint.restore(context())
    lease.release()
    replacement = ActorLease.claim(lease.database, lease.owner, lease.run_id)
    assert replacement is not None
    fresh = OwnedLoopCheckpoint(replacement)
    with pytest.raises(RuntimeSafetyError, match="no longer available"):
        fresh.restore(context())
    with pytest.raises(RuntimeSafetyError, match="Restore existing"):
        await fresh.save(state, [])


@pytest.mark.asyncio
async def test_settled_receipt_authorization_precedes_pending_siblings(lease):
    from workgate.hosted_chat.loop_receipts import execute_checkpointed_batch

    checkpoint = OwnedLoopCheckpoint(lease)
    calls = [
        ModelToolCall("one", "read", {}),
        ModelToolCall("two", "write", {}),
    ]
    state = AgentLoopState(
        context(),
        [],
        step=2,
        model_turn=1,
        pending_calls=calls,
        tool_step_started=True,
    )
    await checkpoint.save(state, calls)
    checkpoint.saver.put_writes(
        checkpoint.active,
        [
            (
                "tool_receipt",
                {"proposal": {"name": "read", "args": {}}, "output": "private"},
            )
        ],
        "receipt:one",
    )
    revoked = False
    seen = []

    async def execute(call):
        seen.append(call.call_id)
        return "new"

    async def preflight(unsettled):
        nonlocal revoked
        assert unsettled == [calls[1]]
        revoked = True

    def authorize(call, output):
        if revoked and call.call_id == "one":
            raise PermissionError("receipt revoked")

    with pytest.raises(PermissionError, match="receipt revoked"):
        await execute_checkpointed_batch(
            checkpoint,
            calls,
            execute,
            check_grant=lambda: None,
            preflight_unsettled=preflight,
            authorize_receipt=authorize,
        )
    assert seen == []
