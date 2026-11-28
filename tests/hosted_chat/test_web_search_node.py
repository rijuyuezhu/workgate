"""Composer admission, owner preference and live grant for native web search."""

import json

import pytest
from fastapi.testclient import TestClient

from tests.support.hosted_chat_http import runtime_for
from workgate.control import hosted_chat_native_tool_node as native
from workgate.control.http.app import build_http_app
from workgate.hosted_chat.agent_runtime import (
    AgentContext,
    AgentLoopState,
    AgentSuspended,
)
from workgate.hosted_chat.approval_claims import ApprovalClaimStore
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.durable_tasks import DurableTaskRepository
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
from workgate.hosted_chat.provider_common import ModelToolCall, ProviderConfig
from workgate.hosted_chat.storage import WorkspaceRepository
from workgate.hosted_chat.web_research import (
    WEB_SEARCH_CAPABILITY,
    web_search_requested,
)

OWNER = "local-user"


@pytest.mark.parametrize(
    "agent,payload,expected",
    [
        (None, {}, True),
        (None, {"web_search": False}, False),
        (
            None,
            {"web_search": False, "ephemeralAgent": {"web_search": True}},
            True,
        ),
        ({"model_parameters": {"web_search": False}}, {}, False),
        (
            {"model_parameters": {"web_search": False}},
            {"web_search": True},
            True,
        ),
    ],
)
def test_request_precedence(agent, payload, expected):
    assert web_search_requested(agent, payload) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "off",
        "no_scope",
        "no_capability",
        "override",
        "revoked",
        "approve",
    ],
)
async def test_web_search_node(tmp_path, monkeypatch, mode):
    runtime = runtime_for(tmp_path)
    db = SQLiteDatabase(runtime.config.state_dir)
    repo = WorkspaceRepository(db)
    generation, _ = GenerationStore(db).begin(
        OWNER,
        {
            "text": "search",
            "model": "m",
            "web_search": mode not in {"off", "override"},
        },
        native_admission={"scopes": ["shell:read"], "task_machine": None},
    )
    if mode == "override":
        repo.save_setting(OWNER, "conversation.features.global", {"web": True})
    if mode == "approve":
        repo.save_setting(OWNER, "approval.mode", "all")
    lease = ActorLease.claim(db, OWNER, generation.run_id)
    assert lease is not None
    context = AgentContext(
        ProviderConfig("openai", "m", "https://model.invalid", "fake"), []
    )
    call = ModelToolCall("search", "web_search", {"query": "test query"})
    seen = []

    async def search(query):
        seen.append(query)
        if mode == "revoked":
            repo.save_setting(
                OWNER,
                "conversation.permissions." + generation.conversation_id,
                {"web": False},
            )
        return {
            "query": query,
            "results": [
                {
                    "url": "https://example.org/",
                    "title": "Test",
                    "snippet": "Public",
                }
            ],
        }

    monkeypatch.setattr(native, "web_search", search)

    def node(actor):
        return native.NativeLoopToolNode(
            runtime,
            DurableTaskRepository(db),
            OwnedLoopCheckpoint(actor),
            allowed_tools={WEB_SEARCH_CAPABILITY}
            if mode != "no_capability"
            else set(),
            scopes={"shell:read"} if mode != "no_scope" else set(),
            expires=None,
            task_machine=None,
            check_grant=lambda: None,
        )

    current = node(lease)
    state = AgentLoopState(context, [{"role": "user", "content": "search"}])
    await current.refresh_tools(state)
    if mode in {"off", "no_scope", "no_capability"}:
        assert not state.context.tools
        with pytest.raises(PermissionError):
            current.preflight([call], state)
        return
    assert [tool["function"]["name"] for tool in state.context.tools] == [
        "web_search"
    ]
    current.preflight([call], state)
    state.step = state.model_turn = 1
    state.pending_calls = [call]
    state.tool_step_started = True
    await current.checkpoint.save(state, [call])
    if mode == "approve":
        with pytest.raises(AgentSuspended):
            await current.dispatch([call], state)
        assert not seen
        approvals = ApprovalClaimStore(db)
        pending = approvals.pending(OWNER, generation.run_id)
        assert pending is not None
        approvals.claim(
            OWNER,
            generation.run_id,
            pending["actionId"],
            [{"tool_call_id": call.call_id, "decision": "approve"}],
        )
        resumed = ActorLease.claim(db, OWNER, generation.run_id)
        assert resumed is not None
        current = node(resumed)
        restored = current.checkpoint.restore(context)
        assert restored is not None
        state = restored.state
    if mode == "revoked":
        with pytest.raises(PermissionError):
            await current.dispatch([call], state)
        assert seen == ["test query"]
        return
    result = await current.dispatch([call], state)
    assert (
        json.loads(result.messages[0]["content"])["results"][0]["url"]
        == "https://example.org/"
    )
    assert (await current.dispatch([call], state)).messages == result.messages
    assert seen == ["test query"]


def test_web_feature_settings_preserve_other_fields(tmp_path):
    runtime = runtime_for(tmp_path)
    db = SQLiteDatabase(runtime.config.state_dir)
    repo = WorkspaceRepository(db)
    generation, _ = GenerationStore(db).begin(
        OWNER, {"text": "test", "model": "m"}
    )
    repo.save_setting(OWNER, "conversation.features.global", {"memory": False})
    prefix = "/api/ui/chat"
    with TestClient(build_http_app(runtime=runtime)) as client:
        global_url = prefix + "/settings/features"
        chat_url = (
            prefix
            + "/conversations/"
            + generation.conversation_id
            + "/features"
        )
        assert client.get(global_url).json() == {"web": True}
        assert client.put(global_url, json={"web": False}).json() == {
            "web": False
        }
        assert repo.setting(OWNER, "conversation.features.global") == {
            "memory": False,
            "web": False,
        }
        assert client.get(chat_url).json() == {"web": False}
        assert client.put(chat_url, json={"web": True}).json() == {"web": True}
        assert client.put(chat_url, json={"web": 1}).status_code == 400
        assert (
            client.get(prefix + "/conversations/missing/features").status_code
            == 404
        )
