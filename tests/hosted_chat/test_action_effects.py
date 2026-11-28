"""External Action proposals and receipts share the owned foreground edge."""

import asyncio
import json

import pytest

from workgate.hosted_chat import action_effects
from workgate.hosted_chat.action_spec import normalize_action_tool_name
from workgate.hosted_chat.actions import ActionRepository
from workgate.hosted_chat.agent_runtime import (
    AgentContext,
    AgentLoopState,
    RuntimeSafetyError,
)
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.approval_claims import ApprovalClaimStore
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from workgate.hosted_chat.provider_common import ModelToolCall, ProviderConfig


def body():
    return {
        "metadata": {
            "domain": "https://api.example.org",
            "raw_spec": json.dumps(
                {
                    "servers": [{"url": "https://api.example.org"}],
                    "paths": {
                        "/write": {
                            "post": {
                                "operationId": "write",
                                "parameters": [
                                    {
                                        "name": "value",
                                        "in": "query",
                                        "required": True,
                                        "schema": {"type": "string"},
                                    },
                                ],
                            }
                        }
                    },
                }
            ),
            "auth": {"type": "none"},
        },
        "functions": [{"function": {"name": "write"}}],
    }


@pytest.fixture
async def started(tmp_path):
    db = SQLiteDatabase(tmp_path)
    agent = AgentRepository(db).create(
        "alice", {"name": "Agent", "provider": "openai", "model": "m"}
    )
    agent, saved = ActionRepository(db).save("alice", agent["id"], body())
    job, _ = GenerationStore(db).begin(
        "alice", {"text": "write", "agent_id": agent["id"], "model": "m"}
    )
    lease = ActorLease.claim(db, "alice", job.run_id)
    assert lease is not None
    checkpoint = OwnedLoopCheckpoint(lease)
    context = AgentContext(
        ProviderConfig("openai", "m", "https://model.invalid", "fake"),
        [],
        agent=agent,
    )
    call = ModelToolCall(
        "effect",
        normalize_action_tool_name(agent["tools"][0]),
        {"value": "one"},
    )
    state = AgentLoopState(
        context,
        [{"role": "user", "content": "write"}],
        step=1,
        model_turn=1,
        pending_calls=[call],
        tool_step_started=True,
    )
    await checkpoint.save(state, [call])
    return db, job, checkpoint, context, call, agent, saved


def grant():
    return {"shell:read", "shell:write"}, None


def approve(started, *, decision="approve"):
    db, job, checkpoint, context, call, _, _ = started
    effects = action_effects.ActionEffects(checkpoint, current_grant=grant)
    effects.bind(call)
    store = ApprovalClaimStore(db)
    pending = store.pause(checkpoint, [call])
    resolution = {"tool_call_id": call.call_id, "decision": decision}
    if decision == "edit":
        resolution["editedArguments"] = {"value": "other"}
    store.claim("alice", job.run_id, pending["actionId"], [resolution])
    lease = ActorLease.claim(db, "alice", job.run_id)
    assert lease is not None
    resumed = OwnedLoopCheckpoint(lease)
    assert resumed.restore(context) is not None
    return action_effects.ActionEffects(resumed, current_grant=grant)


@pytest.mark.asyncio
async def test_exact_approved_action_executes_once(started, monkeypatch):
    db, _, checkpoint, _, call, _, _ = started
    effects = action_effects.ActionEffects(checkpoint, current_grant=grant)
    with pytest.raises(PermissionError):
        await effects.execute(call)
    effects.bind(call)
    effects.bind(call)
    with pytest.raises(PermissionError):
        await effects.execute(call)
    effects = approve(started)
    seen = []

    async def execute(meta, creds, op, args):
        seen.append(args)
        with db.read_snapshot() as conn:
            prior = conn.execute(
                "SELECT receipt FROM agent_native_effect_calls"
            ).fetchone()
            assert prior is not None and prior[0] is None
        return "done"

    monkeypatch.setattr(action_effects, "execute_action", execute)
    result = await effects.execute(call)
    assert result == {"content": [{"type": "text", "text": "done"}]}
    assert effects.receipt(call) == result
    assert await effects.execute(call) == result
    assert seen == [call.arguments]
    with pytest.raises(PermissionError):
        await effects.execute(
            ModelToolCall(call.call_id, call.name, {"value": "other"})
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["timeout", "cancel", "lost_settlement", "revoked"]
)
async def test_uncertain_effect_never_reoffered(started, monkeypatch, mode):
    _, _, _, _, call, _, _ = started
    effects = approve(started)
    seen = []
    revoked = False

    def check():
        return (
            {"shell:read"} if revoked else {"shell:read", "shell:write"}
        ), None

    effects.current_grant = check

    async def execute(*args):
        nonlocal revoked
        seen.append(args)
        if mode == "timeout":
            return "API call failed (timeout)"
        if mode == "cancel":
            raise asyncio.CancelledError()
        if mode == "revoked":
            revoked = True
        return "done"

    monkeypatch.setattr(action_effects, "execute_action", execute)
    original = effects._claim
    if mode == "lost_settlement":

        def lost(*args, **kwargs):
            if kwargs.get("result") is not None:
                raise OSError("lost settlement")
            return original(*args, **kwargs)

        monkeypatch.setattr(effects, "_claim", lost)
    error = {
        "timeout": RuntimeSafetyError,
        "cancel": asyncio.CancelledError,
        "lost_settlement": OSError,
        "revoked": PermissionError,
    }[mode]
    with pytest.raises(error):
        await effects.execute(call)
    revoked = False
    monkeypatch.setattr(effects, "_claim", original)
    with pytest.raises(RuntimeSafetyError, match="uncertain"):
        await effects.execute(call)
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["edited", "rejected", "changed", "missing"])
async def test_action_approval_and_version_fence(started, monkeypatch, mode):
    db, _, _, _, call, agent, saved = started
    effects = approve(
        started,
        decision={"edited": "edit", "rejected": "reject"}.get(mode, "approve"),
    )
    if mode == "changed":
        patch = body()
        patch["action_id"] = saved["action_id"]
        ActionRepository(db).save("alice", agent["id"], patch)
    if mode == "missing":
        with db.connect() as conn:
            conn.execute(
                "UPDATE runs SET state=json_remove(state,'$.actionProposals')"
            )

    async def never(*args):
        pytest.fail("Unauthorized Action sent")

    monkeypatch.setattr(action_effects, "execute_action", never)
    with pytest.raises(PermissionError):
        await effects.execute(call)
    with db.read_snapshot() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_native_effect_calls"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_effect_ledger_collision_and_replay_requires_receipt(started):
    db, job, _, _, call, _, _ = started
    effects = approve(started)
    with pytest.raises(PermissionError, match="Missing settled"):
        effects.receipt(call)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO agent_native_effect_calls(run_id,call_id,fingerprint,machine,scopes) VALUES(?,?,?,?,?)",
            (
                job.run_id,
                call.call_id,
                "other",
                "control",
                '["shell:read","shell:write"]',
            ),
        )
    with pytest.raises(PermissionError, match="identity changed"):
        await effects.execute(call)


@pytest.mark.asyncio
async def test_http_failure_stops_uncertain_action(started, monkeypatch):
    _, _, _, _, call, _, _ = started
    effects = approve(started)

    async def failed(*args):
        return "API call failed (HTTP 500)"

    monkeypatch.setattr(action_effects, "execute_action", failed)
    with pytest.raises(RuntimeSafetyError, match="uncertain"):
        await effects.execute(call)
    with pytest.raises(RuntimeSafetyError, match="uncertain"):
        await effects.execute(call)
