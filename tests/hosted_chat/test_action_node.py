"""The foreground node advertises and executes only reviewed owned Actions."""

import json

import pytest

from tests.support.hosted_chat_http import runtime_for
from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
from workgate.hosted_chat import action_effects
from workgate.hosted_chat.action_effects import ACTION_CAPABILITY
from workgate.hosted_chat.action_spec import normalize_action_tool_name
from workgate.hosted_chat.actions import ActionRepository
from workgate.hosted_chat.agent_runtime import (
    AgentContext,
    AgentLoopState,
    AgentSuspended,
)
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.approval_claims import ApprovalClaimStore
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.durable_tasks import DurableTaskRepository
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from workgate.hosted_chat.provider_common import ModelToolCall, ProviderConfig
from workgate.hosted_chat.storage import WorkspaceRepository

OWNER = "local-user"
SCOPES = {"shell:read", "shell:write"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["approve", "reject", "denied", "read_only", "no_capability", "oauth"],
)
async def test_foreground_action_admission_approval_and_replay(
    tmp_path, monkeypatch, mode
):
    runtime = runtime_for(tmp_path)
    db = SQLiteDatabase(runtime.config.state_dir)
    agent = AgentRepository(db).create(
        OWNER, {"name": "Agent", "provider": "openai", "model": "m"}
    )
    metadata = {
        "domain": "https://example.org",
        "raw_spec": json.dumps(
            {
                "servers": [{"url": "https://example.org/v1"}],
                "paths": {
                    "/write/{key}": {
                        "post": {
                            "operationId": "write",
                            "parameters": [
                                {
                                    "name": "key",
                                    "in": "path",
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
    }
    if mode == "oauth":
        metadata.update(
            {
                "auth": {
                    "type": "oauth",
                    "token_exchange_method": "default_post",
                    "scope": "read",
                    "authorization_url": "https://auth.example.org/authorize",
                    "client_url": "https://auth.example.org/token",
                },
                "oauth_client_id": "client",
                "oauth_client_secret": "secret",
            }
        )
    agent, _ = ActionRepository(db).save(
        OWNER,
        agent["id"],
        {"metadata": metadata, "functions": [{"function": {"name": "write"}}]},
    )
    job, _ = GenerationStore(db).begin(
        OWNER, {"text": "write", "agent_id": agent["id"], "model": "m"}
    )
    lease = ActorLease.claim(db, OWNER, job.run_id)
    assert lease is not None
    context = AgentContext(
        ProviderConfig("openai", "m", "https://model.invalid", "fake"),
        [],
        agent=agent,
    )
    call = ModelToolCall(
        "action", normalize_action_tool_name(agent["tools"][0]), {"key": "a"}
    )
    calls = []

    async def execute(meta, creds, op, args):
        calls.append((op["action_id"], args))
        return "done"

    monkeypatch.setattr(action_effects, "execute_action", execute)

    def node(actor):
        return NativeLoopToolNode(
            runtime,
            DurableTaskRepository(db),
            OwnedLoopCheckpoint(actor),
            allowed_tools={ACTION_CAPABILITY}
            if mode != "no_capability"
            else set(),
            scopes=SCOPES if mode != "read_only" else {"shell:read"},
            expires=None,
            task_machine=None,
            check_grant=lambda: None,
        )

    if mode == "denied":
        WorkspaceRepository(db).save_setting(
            OWNER,
            "endpoints",
            {
                "agents": {
                    "toolApproval": {"enabled": True, "deny": [call.name]}
                }
            },
        )
    current = node(lease)
    state = AgentLoopState(context, [{"role": "user", "content": "write"}])
    await current.refresh_tools(state)
    names = [tool["function"]["name"] for tool in state.context.tools]
    if mode in {"read_only", "no_capability", "oauth"}:
        assert names == []
        with pytest.raises(PermissionError, match="advertised"):
            current.preflight([call], state)
        return
    assert names == [call.name]
    current.preflight([call], state)
    state.step = state.model_turn = 1
    state.pending_calls = [call]
    state.tool_step_started = True
    await current.checkpoint.save(state, [call])
    if mode == "denied":
        result = await current.dispatch([call], state)
        assert result.messages[0]["is_error"]
        assert not calls
        return
    with pytest.raises(AgentSuspended, match="approval"):
        await current.dispatch([call], state)
    store = ApprovalClaimStore(db)
    pending = store.pending(OWNER, job.run_id)
    assert pending is not None
    request = pending["payload"]["action_requests"][0]
    assert (
        request["description"]
        == "POST https://example.org/v1/write/a (authentication: none)"
    )
    assert set(
        pending["payload"]["review_configs"][0]["allowed_decisions"]
    ) == {"approve", "reject"}
    assert not calls
    store.claim(
        OWNER,
        job.run_id,
        pending["actionId"],
        [{"tool_call_id": call.call_id, "decision": mode}],
    )
    resumed = ActorLease.claim(db, OWNER, job.run_id)
    assert resumed is not None
    current = node(resumed)
    restored = current.checkpoint.restore(context)
    assert restored is not None
    result = await current.dispatch([call], restored.state)
    if mode == "reject":
        assert result.messages[0]["is_error"]
        assert not calls
    else:
        assert result.messages[0]["content"] == "done"
        assert len(calls) == 1
    assert (
        await current.dispatch([call], restored.state)
    ).messages == result.messages
    assert len(calls) == (1 if mode == "approve" else 0)
