"""End-to-end automatic summary requests remain separate from answer history."""

import json

import pytest
from fastapi.testclient import TestClient

from tests.support.hosted_chat_http import provider, runtime_for
from workgate.control.http import hosted_chat
from workgate.control.http.app import build_http_app
from workgate.hosted_chat.credential_cipher import CredentialCipher
from workgate.hosted_chat.provider_common import (
    ModelFinish,
    ModelThought,
    ModelToolCall,
    ModelUsage,
)
from workgate.hosted_chat.provider_profiles import ProviderRepository
from workgate.hosted_chat.storage import WorkspaceRepository


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "disabled",
        "truncated",
        "tool",
        "empty",
        "oversized",
        "revoked",
    ],
)
def test_automatic_compaction_http(tmp_path, monkeypatch, mode):
    runtime = runtime_for(tmp_path)
    db = provider(runtime)
    ProviderRepository(db, CredentialCipher(db)).save(
        {
            "name": "test-provider",
            "provider": "openai",
            "models": ["test-model"],
            "model_contexts": {"test-model": 8192},
            "base_url": "https://models.invalid/v1",
            "api_key": "test-secret",
        }
    )
    repo = WorkspaceRepository(db)
    settings = {
        "summarization": {
            "enabled": mode != "disabled",
            "retainRecent": {"turns": 1},
            "trigger": {"type": "messages_to_refine", "value": 2},
            "parameters": {"maxSummaryTokens": 128},
        }
    }
    repo.save_setting("local-user", "endpoints", settings)
    original = hosted_chat.run_authenticated_generation
    summaries = []
    primary = []

    async def stream(profile, messages, **kwargs):
        if kwargs.get("emit_finish_reason"):
            summaries.append(messages)
            assert not kwargs.get("tools")
            if mode == "truncated":
                yield "short"
                yield ModelFinish("openai", "length")
            elif mode == "tool":
                yield ModelToolCall("call", "task", {})
            elif mode == "empty":
                yield ""
            elif mode == "oversized":
                yield "word " * 500
            else:
                yield ModelThought("thinking", text="not summary")
                yield ModelUsage("openai", 100, 20)
                yield "Earlier context."
                yield ModelFinish("openai", "stop")
                if mode == "revoked":
                    repo.save_setting(
                        "local-user",
                        "endpoints",
                        {"summarization": {"enabled": False}},
                    )
            return
        primary.append(messages)
        yield "Answer " * 100

    async def execute(*args, **kwargs):
        return await original(
            *args, **kwargs, stream_tools=stream, stream_text=stream
        )

    monkeypatch.setattr(hosted_chat, "run_authenticated_generation", execute)
    with TestClient(build_http_app(runtime=runtime)) as client:
        cid = None
        events = []
        for index in range(2):
            body = {
                "text": f"Question {index} " + "detail " * 160,
                "provider": "test-provider",
                "model": "test-model",
            }
            if cid is not None:
                body["conversationId"] = cid
            result = client.post("/api/ui/chat/generations", json=body)
            assert result.status_code == 202, result.text
            cid = result.json()["conversationId"]
            response = client.get(
                f"/api/ui/chat/generations/{result.json()['runId']}/events"
            )
            events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            assert (
                events[-1]["final"]
                and "Answer" in events[-1]["responseMessage"]["text"]
            )
        assert len(summaries) == (0 if mode == "disabled" else 1)
        assert len(primary) == 2
        if mode == "success":
            assert any(
                "Earlier context." in message["content"]
                for message in primary[-1]
            )
            assert (
                len(
                    [
                        message
                        for message in primary[-1]
                        if message["role"] != "system"
                    ]
                )
                == 1
            )
            assert any(
                event.get("event") == "on_compaction" for event in events
            )
        else:
            assert not any(
                "Earlier context." in str(message) for message in primary[-1]
            )
            assert not any(
                event.get("event") == "on_compaction" for event in events
            )
        assert cid is not None
        assert len(repo.messages("local-user", cid)["messages"]) == 4
        assert "Earlier context." not in json.dumps(
            repo.messages("local-user", cid)
        )


@pytest.mark.parametrize("mode", ["success", "disabled", "truncated"])
def test_manual_compaction_anchored_http(tmp_path, monkeypatch, mode):
    runtime = runtime_for(tmp_path)
    db = provider(runtime)
    repo = WorkspaceRepository(db)
    config: dict = {"parameters": {"maxSummaryTokens": 128}}
    if mode == "disabled":
        config["enabled"] = False
    repo.save_setting("local-user", "endpoints", {"summarization": config})
    original = hosted_chat.run_authenticated_generation
    summaries = []
    primary = []

    async def stream(profile, messages, **kwargs):
        if kwargs.get("emit_finish_reason"):
            summaries.append(messages)
            assert not kwargs.get("tools")
            yield "Earlier context."
            yield ModelFinish(
                "openai", "length" if mode == "truncated" else "stop"
            )
        else:
            primary.append(messages)
            yield "Answer " * 100

    async def execute(*args, **kwargs):
        return await original(
            *args, **kwargs, stream_tools=stream, stream_text=stream
        )

    monkeypatch.setattr(hosted_chat, "run_authenticated_generation", execute)
    prefix = "/api/ui/chat/generations"
    with TestClient(build_http_app(runtime=runtime)) as client:
        first = client.post(
            prefix,
            json={
                "text": "Question " + "detail " * 160,
                "provider": "test-provider",
                "model": "test-model",
            },
        )
        assert first.status_code == 202
        cid = first.json()["conversationId"]
        client.get(f"{prefix}/{first.json()['runId']}/events")
        before = repo.messages("local-user", cid)["messages"]
        anchor = before[-1]["messageId"]
        assert client.post(prefix, json={"compact": True}).status_code == 400
        assert (
            client.post(
                prefix,
                json={
                    "compact": True,
                    "conversationId": cid,
                    "parentMessageId": "not-owned",
                },
            ).status_code
            == 404
        )
        body = {
            "compact": True,
            "conversationId": cid,
            "parentMessageId": anchor,
            "clientRequestId": "manual",
        }
        response = client.post(prefix, json=body)
        assert response.status_code == 202, response.text
        run_id = response.json()["runId"]
        events = [
            json.loads(line[6:])
            for line in client.get(
                f"{prefix}/{run_id}/events"
            ).text.splitlines()
            if line.startswith("data: ")
        ]
        assert events[0]["message"]["compact"] is True
        assert events[0]["message"]["messageId"] == anchor
        final = events[-1]["responseMessage"]
        assert len(primary) == 1
        assert len(summaries) == (0 if mode == "disabled" else 1)
        after = repo.messages("local-user", cid)["messages"]
        assert after[:2] == before
        assert len(after) == 3
        assert after[-1]["parentMessageId"] == anchor
        assert after[-1]["manualCompaction"] is True
        if mode == "success":
            assert final["content"][0]["type"] == "summary"
            assert final["content"][0]["initiatedBy"] == "user"
            assert (
                final["content"][0]["content"][0]["text"] == "Earlier context."
            )
        else:
            assert final["content"][0]["type"] == "error"
        replay = client.post(prefix, json=body)
        assert replay.json()["runId"] == run_id and replay.json()["resumed"]
        assert (
            client.post(
                prefix, json={**body, "parentMessageId": before[0]["messageId"]}
            ).status_code
            == 400
        )
        next_run = client.post(
            prefix,
            json={
                "text": "Next",
                "conversationId": cid,
                "parentMessageId": after[-1]["messageId"],
            },
        )
        assert next_run.status_code == 202
        client.get(f"{prefix}/{next_run.json()['runId']}/events")
        assert len(primary) == 2
        assert any(
            "Earlier context." in str(message) for message in primary[-1]
        ) == (mode == "success")
        assert not any(
            message.get("role") == "assistant" and not message.get("content")
            for message in primary[-1]
        )
