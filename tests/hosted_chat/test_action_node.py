"""The foreground node advertises and executes only reviewed owned Actions."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.support.hosted_chat_http import runtime_for
from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
from workgate.hosted_chat import action_effects, action_oauth_flow
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
    [
        "approve",
        "reject",
        "denied",
        "read_only",
        "no_capability",
        "oauth",
        "oauth_resume",
        "oauth_cached",
        "oauth_batch",
    ],
)
async def test_foreground_action_admission_approval_and_replay(
    tmp_path, monkeypatch, mode
):
    live_oauth = mode in {"oauth_resume", "oauth_cached", "oauth_batch"}
    base = "https://controller.example.org"
    runtime = runtime_for(tmp_path, base_url=base if live_oauth else None)
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
    if mode == "oauth" or live_oauth:
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
    agent, saved = ActionRepository(db).save(
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
    batch = [call]
    if mode == "oauth_batch":
        batch.append(ModelToolCall("other-action", call.name, {"key": "b"}))
    flows = action_oauth_flow.ActionOAuthFlows(ActionRepository(db))
    if mode == "oauth_cached":
        flows.tokens.store_code_tokens(
            OWNER, saved["action_id"], 1, {"access_token": "test-access"}
        )

    async def execute(meta, creds, op, args):
        if live_oauth:
            assert creds["oauth_access_token"] == "test-access"
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
            current.preflight(batch, state)
        return
    assert names == [call.name]
    current.preflight(batch, state)
    state.step = state.model_turn = 1
    state.pending_calls = batch
    state.tool_step_started = True
    await current.checkpoint.save(state, batch)
    if mode == "denied":
        result = await current.dispatch(batch, state)
        assert result.messages[0]["is_error"]
        assert not calls
        return
    with pytest.raises(AgentSuspended, match="approval"):
        await current.dispatch(batch, state)
    store = ApprovalClaimStore(db)
    pending = store.pending(OWNER, job.run_id)
    assert pending is not None
    request = pending["payload"]["action_requests"][0]
    assert (
        request["description"]
        == f"POST https://example.org/v1/write/a (authentication: {'oauth' if live_oauth else 'none'})"
    )
    assert set(
        pending["payload"]["review_configs"][0]["allowed_decisions"]
    ) == {"approve", "reject"}
    assert not calls
    store.claim(
        OWNER,
        job.run_id,
        pending["actionId"],
        [
            {
                "tool_call_id": item.call_id,
                "decision": "approve" if live_oauth else mode,
            }
            for item in batch
        ],
    )
    resumed = ActorLease.claim(db, OWNER, job.run_id)
    assert resumed is not None
    current = node(resumed)
    restored = current.checkpoint.restore(context)
    assert restored is not None
    if mode in {"oauth_resume", "oauth_batch"}:
        with pytest.raises(AgentSuspended, match="OAuth"):
            await current.dispatch(batch, restored.state)
        assert not calls
        with db.read_snapshot() as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM agent_native_effect_calls"
                ).fetchone()[0]
                == 0
            )
        pending_oauth = store.pending(OWNER, job.run_id)
        assert pending_oauth is not None
        browser = flows.begin(
            OWNER,
            job.run_id,
            pending_oauth["actionId"],
            base,
            check_grant=lambda: None,
        )
        state_key = parse_qs(urlsplit(browser.authorization_url).query)[
            "state"
        ][0]
        claimed = flows.claim(
            state_key, browser.cookie, action_oauth_flow.callback_uri(base)
        )

        async def exchange(*unused):
            return {"access_token": "test-access"}

        monkeypatch.setattr(action_oauth_flow, "exchange", exchange)
        await flows.complete(claimed, "test-code")
        resumed = ActorLease.claim(db, OWNER, job.run_id)
        assert resumed is not None
        current = node(resumed)
        restored = current.checkpoint.restore(context)
        assert restored is not None
    result = await current.dispatch(batch, restored.state)
    if mode == "reject":
        assert result.messages[0]["is_error"]
        assert not calls
    else:
        assert result.messages[0]["content"] == "done"
        assert len(calls) == len(batch)
    assert (
        await current.dispatch(batch, restored.state)
    ).messages == result.messages
    assert len(calls) == (len(batch) if mode == "approve" or live_oauth else 0)
