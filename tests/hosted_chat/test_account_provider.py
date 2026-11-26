"""Account-backed Responses request contracts adapted from pinned v6.0.3."""

import json
import time
from dataclasses import replace

import httpx
import pytest

from workgate.hosted_chat import (
    account_credentials,
    model_oauth,
    provider_common,
)
from workgate.hosted_chat.credential_cipher import CredentialCipher
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.provider_profiles import ProviderRepository
from workgate.hosted_chat.providers import (
    ModelToolCall,
    ProviderConfig,
    provider_config,
    stream_model,
    stream_model_with_tools,
)

pytestmark = pytest.mark.asyncio

TOOLS = [
    {
        "type": "function",
        "function": {"name": "read", "parameters": {"type": "object"}},
    }
]
USER = {"role": "user", "content": "Read file"}


@pytest.fixture
async def account(tmp_path, monkeypatch):
    db = SQLiteDatabase(tmp_path)
    repo = ProviderRepository(db, CredentialCipher(db))

    async def catalog(self, token):
        return {"models": ["model"], "model_contexts": {}}

    monkeypatch.setattr(model_oauth.ChatGPTAuthorization, "catalog", catalog)
    await account_credentials.save_account(
        repo,
        "chatgpt",
        "Account",
        {
            "client_id": "oaiapp_mock",
            "subject": "account",
            "scope": model_oauth.SCOPE,
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "id_token": "mock-id",
            "expires_at": time.time() + 3600,
        },
    )
    return repo


@pytest.fixture
def transport(monkeypatch):
    requests = []
    replies = []
    original = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        status, frames = replies.pop(0)
        body = (
            "\n\n".join("data: " + json.dumps(frame) for frame in frames)
            + "\n\n"
        )
        return httpx.Response(
            status, text=body, headers={"content-type": "text/event-stream"}
        )

    def client(base, **kwargs):
        assert base == model_oauth.RESOURCE + "/responses"
        assert kwargs["allow_private_http"] is False
        return original(
            transport=httpx.MockTransport(handle), timeout=kwargs["timeout"]
        )

    monkeypatch.setattr(provider_common, "provider_client", client)
    return replies, requests


def completed(*items):
    return {
        "type": "response.completed",
        "response": {"status": "completed", "output": list(items)},
    }


def message(text):
    return {
        "type": "message",
        "id": "msg_1",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def call():
    return {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "read",
        "arguments": "{}",
        "status": "completed",
    }


async def test_explicit_account_config_and_pre_stream_refresh(
    account, transport, monkeypatch
):
    with pytest.raises(ValueError, match="not available"):
        provider_config(account, "Account")
    config = provider_config(account, "Account", allow_account=True)
    assert (
        config.token == ""
        and config.account_id
        and config.account_repository is account
    )
    assert "old-access" not in repr(config) and "old-refresh" not in repr(
        config
    )
    scope = provider_common.provider_scope(config)
    calls = []

    async def refresh(self, previous):
        calls.append(previous["refresh_token"])
        return {
            **previous,
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_at": time.time() + 3600,
        }

    monkeypatch.setattr(model_oauth.ChatGPTAuthorization, "refresh", refresh)
    replies, requests = transport
    replies.extend([(401, []), (200, [completed(message("Read"), call())])])
    result = [
        event
        async for event in stream_model_with_tools(
            config,
            [USER],
            tools=TOOLS,
            max_tokens=40,
            temperature=0.3,
            top_p=0.8,
        )
    ]
    assert result[0] == "Read"
    assert any(
        isinstance(event, ModelToolCall) and event.call_id == "call_1"
        for event in result
    )
    assert calls == ["old-refresh"]
    assert [req.headers["Authorization"] for req in requests] == [
        "Bearer old-access",
        "Bearer new-access",
    ]
    for req in requests:
        assert str(req.url) == model_oauth.RESOURCE + "/responses"
        body = json.loads(req.content)
        assert body["store"] is False
        assert body["tools"][0]["type"] == "namespace"
        assert body["tools"][0]["tools"][0]["name"] == "read"
        assert not {"max_output_tokens", "temperature", "top_p"} & body.keys()
    assert (
        provider_common.provider_scope(
            provider_config(account, "Account", allow_account=True)
        )
        == scope
    )
    assert (
        account_credentials.unseal(account, account.profile("Account"))[
            "refresh_token"
        ]
        == "new-refresh"
    )


@pytest.mark.parametrize("status", [401, 403, 500])
async def test_account_retry_is_bounded(
    account, transport, monkeypatch, status
):
    config = provider_config(account, "Account", allow_account=True)
    calls = []

    async def refresh(self, previous):
        calls.append(previous["refresh_token"])
        return {
            **previous,
            "access_token": "new-access",
            "expires_at": time.time() + 3600,
        }

    monkeypatch.setattr(model_oauth.ChatGPTAuthorization, "refresh", refresh)
    replies, requests = transport
    replies.extend([(status, []), (status, [])])
    with pytest.raises(httpx.HTTPStatusError):
        _ = [event async for event in stream_model(config, [USER])]
    assert len(requests) == (2 if status == 401 else 1)
    assert len(calls) == (1 if status == 401 else 0)


async def test_post_output_error_never_retries(account, transport, monkeypatch):
    config = provider_config(account, "Account", allow_account=True)

    async def unexpected(*args):
        pytest.fail("must not refresh after output")

    monkeypatch.setattr(model_oauth.ChatGPTAuthorization, "refresh", unexpected)
    replies, requests = transport
    replies.append(
        (
            200,
            [
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "delta": "partial",
                },
                {"type": "response.failed"},
            ],
        )
    )
    events = []
    with pytest.raises(RuntimeError, match="rejected"):
        async for event in stream_model(config, [USER]):
            events.append(event)
    assert events == ["partial"]
    assert len(requests) == 1


async def test_account_endpoint_and_capability_cannot_be_changed(
    account, transport
):
    config = provider_config(account, "Account", allow_account=True)
    for altered in (
        replace(config, url="https://elsewhere.invalid/v1/responses"),
        replace(config, url=model_oauth.RESOURCE + "/responses?other=1"),
        replace(config, provider="openai"),
        replace(config, token="api-key"),
        replace(config, allow_private_http=True),
        replace(config, account_repository=None),
        replace(config, account_id=None),
        replace(config, auth_method="unsupported"),
        replace(config, account_channel=None),
        replace(config, channel="Other"),
    ):
        with pytest.raises(ValueError):
            _ = [event async for event in stream_model(altered, [USER])]
    assert transport[1] == []


async def test_deleted_account_cannot_dispatch(account, transport):
    config = provider_config(account, "Account", allow_account=True)
    account.delete(name="Account")
    with pytest.raises(model_oauth.ReauthorizationRequired):
        _ = [event async for event in stream_model(config, [USER])]
    assert transport[1] == []


async def test_no_account_fallback_without_explicit_capability():
    config = ProviderConfig(
        "openai-responses",
        "model",
        model_oauth.RESOURCE + "/responses",
        "",
        account_id="id",
    )
    with pytest.raises(ValueError, match="not available"):
        _ = [event async for event in stream_model(config, [USER])]


async def test_reauthorized_account_invalidates_old_configuration(
    account, transport
):
    config = provider_config(account, "Account", allow_account=True)
    original = account.profile("Account")
    credentials = account_credentials.unseal(account, original)
    await account_credentials.save_account(
        account,
        "chatgpt",
        "Account",
        {**credentials, "access_token": "reauthorized-access"},
        original=original,
    )
    with pytest.raises(model_oauth.ReauthorizationRequired):
        _ = [event async for event in stream_model(config, [USER])]
    assert transport[1] == []
