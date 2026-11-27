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
