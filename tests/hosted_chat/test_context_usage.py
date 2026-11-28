"""Provider usage is durable, owner-bound and contains counts only."""

import json
from dataclasses import replace

import pytest

from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
from workgate.hosted_chat.context_usage import record_root_context_usage
from workgate.hosted_chat.credential_cipher import CredentialCipher
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generation_actor_lease import (
    ActorLease,
    ActorLeaseLost,
)
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.provider_common import ModelUsage, ProviderConfig
from workgate.hosted_chat.provider_profiles import ProviderRepository
from workgate.hosted_chat.storage import WorkspaceRepository


@pytest.fixture
def bound(tmp_path):
    database = SQLiteDatabase(tmp_path)
    ProviderRepository(database, CredentialCipher(database)).save(
        {
            "name": "channel",
            "provider": "openai",
            "base_url": "https://example.invalid/v1",
            "models": ["m"],
            "model_contexts": {"m": 4096},
            "api_key": "secret",
        }
    )
    store = GenerationStore(database)
    job, _ = store.begin(
        "alice", {"text": "private text", "endpoint": "channel", "model": "m"}
    )
    lease = ActorLease.claim(database, "alice", job.run_id)
    assert lease is not None
    context = AgentContext(
        ProviderConfig(
            "openai",
            "m",
            "https://example.invalid",
            "secret",
            channel="channel",
        ),
        [],
    )
    state = AgentLoopState(
        context, [{"role": "user", "content": "private text"}], model_turn=1
    )
    state.provider_projection = [
        {"role": "system", "content": "private memory"},
        *state.messages,
    ]
    return store, job, lease, state


def test_snapshot_atomic_replay_and_final_metadata(bound):
    store, job, lease, state = bound
    usage = ModelUsage("openai", 100, 20)
    assert record_root_context_usage(lease, state, usage)
    assert not record_root_context_usage(lease, state, usage)
    event = store.events("alice", job.run_id)[-1][1]
    assert event["event"] == "on_context_usage"
    snapshot = event["data"]
    assert snapshot["providerInputTokens"] == 100
    assert snapshot["breakdown"]["maxContextTokens"] == 4096
    assert "private" not in json.dumps(snapshot) and "secret" not in json.dumps(
        snapshot
    )
    assert not store.events("bob", job.run_id)
    state.model_turn += 1
    state.context.parameters["max_context_tokens"] = 2048
    assert record_root_context_usage(lease, state, usage)
    last = store.events("alice", job.run_id)[-1][1]["data"]
    assert last["breakdown"]["maxContextTokens"] == 2048
    assert store.finish(
        "alice", job.run_id, status="complete", text="Done", actor_lease=lease
    )
    final = store.events("alice", job.run_id)[-1][1]
    assert final["responseMessage"]["metadata"]["contextUsage"] == last
    assert (
        WorkspaceRepository(store.database).messages(
            "alice", job.conversation_id
        )["messages"][-1]["metadata"]["contextUsage"]
        == last
    )
    with pytest.raises(ActorLeaseLost):
        record_root_context_usage(lease, state, usage)


def test_usage_rejects_invalid_and_private_observations(bound):
    store, job, lease, state = bound
    usage = ModelUsage("openai", 100, 20)
    assert not record_root_context_usage(
        replace(lease, detached_task_id="child"), state, usage
    )
    assert not record_root_context_usage(
        replace(lease, wakeup_task_id="wake"), state, usage
    )
    for invalid in (
        ModelUsage("google", 100, 20),
        ModelUsage("openai", 0, 20),
        ModelUsage("openai", 100, -1),
    ):
        assert not record_root_context_usage(lease, state, invalid)
    projection = state.provider_projection
    state.provider_projection = None
    assert not record_root_context_usage(lease, state, usage)
    state.provider_projection = projection
    state.model_turn = 0
    assert not record_root_context_usage(lease, state, usage)
    state.model_turn = 1
    state.context.profile = replace(state.context.profile, model="wrong")
    assert not record_root_context_usage(lease, state, usage)
    assert len(store.events("alice", job.run_id)) == 1


def test_usage_setting_limit_and_atomic_failure(bound, monkeypatch):
    from workgate.hosted_chat import context_usage

    store, job, lease, state = bound
    repo = WorkspaceRepository(store.database)
    repo.save_setting(
        "alice",
        "endpoints",
        {
            "agents": {"maxContextTokens": 1024},
            "summarization": {"reserveRatio": 2},
        },
    )
    assert record_root_context_usage(
        lease, state, ModelUsage("openai", 100, 20)
    )
    snap = store.events("alice", job.run_id)[-1][1]["data"]
    assert snap["breakdown"]["maxContextTokens"] == 1024
    state.model_turn += 1
    monkeypatch.setattr(context_usage, "EVENT_LIMIT", 1)
    with pytest.raises(ValueError, match="retention limit"):
        record_root_context_usage(lease, state, ModelUsage("openai", 100, 20))
    with store.database.read_snapshot() as db:
        run = json.loads(
            db.execute(
                "SELECT state FROM runs WHERE id=?", (job.run_id,)
            ).fetchone()[0]
        )
    assert run["contextUsageTurn"] == 1
    assert run["contextUsage"] == snap
    monkeypatch.setattr(context_usage, "EVENT_LIMIT", 100)
    monkeypatch.setattr(context_usage, "MAX_EVENT_BYTES", 1)
    with pytest.raises(ValueError, match="retention limit"):
        record_root_context_usage(lease, state, ModelUsage("openai", 100, 20))
    assert len(store.events("alice", job.run_id)) == 2


def test_foreground_usage_http_replay(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tests.support.hosted_chat_http import provider, runtime_for
    from workgate.control.http import hosted_chat
    from workgate.control.http.app import build_http_app

    runtime = runtime_for(tmp_path)
    provider(runtime)
    original = hosted_chat.run_authenticated_generation

    async def stream(*args, **kwargs):
        yield "Answer"
        yield ModelUsage("openai", 500, 4)

    async def execute(*args, **kwargs):
        return await original(
            *args, **kwargs, stream_tools=stream, stream_text=stream
        )

    monkeypatch.setattr(hosted_chat, "run_authenticated_generation", execute)
    with TestClient(build_http_app(runtime=runtime)) as client:
        response = client.post(
            "/api/ui/chat/generations",
            json={
                "text": "Question",
                "provider": "test-provider",
                "model": "test-model",
            },
        )
        assert response.status_code == 202
        run_id = response.json()["runId"]
        events = client.get(f"/api/ui/chat/generations/{run_id}/events")
        records = [
            json.loads(line[6:])
            for line in events.text.splitlines()
            if line.startswith("data: ")
        ]
        snapshot = next(
            item["data"]
            for item in records
            if item.get("event") == "on_context_usage"
        )
        assert snapshot["providerInputTokens"] == 500
        assert snapshot["breakdown"]["maxContextTokens"] == 0
        assert (
            records[-1]["responseMessage"]["metadata"]["contextUsage"]
            == snapshot
        )
        assert "test-secret" not in events.text
