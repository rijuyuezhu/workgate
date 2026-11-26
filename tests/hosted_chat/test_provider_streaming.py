"""Final provider text-stream contracts, adapted to explicit Workgate profiles."""

import json

import httpx
import pytest

from workgate.hosted_chat import provider_common
from workgate.hosted_chat.providers import (
    ModelFinish,
    ProviderConfig,
    stream_model,
)


async def _collect(stream):
    return [part async for part in stream]


def _mock_client(monkeypatch, frames, captured):
    original = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            200,
            text="\n\n".join(frames) + "\n\n",
            headers={"content-type": "text/event-stream"},
        )

    def factory(_base, **kwargs):
        return original(
            transport=httpx.MockTransport(handler), timeout=kwargs["timeout"]
        )

    monkeypatch.setattr(provider_common, "provider_client", factory)


@pytest.mark.asyncio
async def test_openai_chat_stream_parses_multiple_chunks(monkeypatch):
    captured: dict = {}
    frames = [
        'data: {"choices":[{"delta":{"content":"Hello"}}]}',
        'data: {"choices":[{"delta":{"content":" world"}}]}',
        "data: [DONE]",
    ]
    _mock_client(monkeypatch, frames, captured)
    config = ProviderConfig(
        "openai",
        "test-model",
        "https://api.example.invalid/v1/chat/completions",
        "private",
    )
    actual = await _collect(
        stream_model(config, [{"role": "user", "content": "Hi"}])
    )
    assert actual == ["Hello", " world"]
    assert captured["request"].headers["authorization"] == "Bearer private"
    body = json.loads(captured["request"].content)
    assert body["messages"][0]["role"] == "user"
    assert body["stream"] is True


@pytest.mark.asyncio
async def test_anthropic_stream_and_system_message(monkeypatch):
    captured: dict = {}
    _mock_client(
        monkeypatch,
        [
            'data: {"type":"message_start"}',
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"text"}}',
        ],
        captured,
    )
    config = ProviderConfig(
        "anthropic",
        "claude-test",
        "https://api.example.invalid/v1/messages",
        "secret",
    )
    actual = await _collect(
        stream_model(
            config,
            [
                {"role": "system", "content": "Be concise"},
                {"role": "user", "content": "Hello"},
            ],
        )
    )
    assert actual == ["text"]
    assert captured["request"].headers["x-api-key"] == "secret"
    payload = json.loads(captured["request"].content)
    assert payload["system"] == "Be concise"
    assert payload["messages"] == [{"role": "user", "content": "Hello"}]


@pytest.mark.asyncio
async def test_gemini_stream_and_model_roles(monkeypatch):
    captured: dict = {}
    _mock_client(
        monkeypatch,
        [
            'data: {"candidates":[{"content":{"parts":[{"text":"token"}]}}]}',
        ],
        captured,
    )
    config = ProviderConfig(
        "google",
        "gemini-test",
        "https://api.example.invalid:443/path",
        "google-key",
    )
    result = await _collect(
        stream_model(
            config,
            [
                {"role": "system", "content": "Be concise"},
                {"role": "assistant", "content": "Okay"},
                {"role": "user", "content": "Hello"},
            ],
        )
    )
    assert result == ["token"]
    assert captured["request"].headers["x-goog-api-key"] == "google-key"
    payload = json.loads(captured["request"].content)
    assert payload["systemInstruction"]["parts"][0]["text"] == "Be concise"
    assert [item["role"] for item in payload["contents"]] == ["model", "user"]


@pytest.mark.asyncio
async def test_provider_error_does_not_echo_secrets(monkeypatch):
    captured: dict = {}
    _mock_client(
        monkeypatch, ['data: {"error":{"message":"secret rejected"}}'], captured
    )
    config = ProviderConfig(
        "openai",
        "test",
        "https://api.example.invalid/v1/chat/completions",
        "very-private",
    )
    with pytest.raises(RuntimeError, match="Model provider rejected") as exc:
        await _collect(
            stream_model(config, [{"role": "user", "content": "Hello"}])
        )
    assert "very-private" not in str(exc.value)


@pytest.mark.asyncio
async def test_loopback_openai_with_no_key_omits_authorization(monkeypatch):
    captured = {}
    _mock_client(
        monkeypatch,
        ['data: {"choices":[{"delta":{"content":"ok"}}]}', "data: [DONE]"],
        captured,
    )
    config = ProviderConfig(
        "openai", "local", "http://127.0.0.1:11434/v1/chat/completions", ""
    )
    assert await _collect(
        stream_model(config, [{"role": "user", "content": "hi"}])
    ) == ["ok"]
    assert "authorization" not in captured["request"].headers


@pytest.mark.asyncio
async def test_openai_stream_stop_reason_is_available_only_when_requested(
    monkeypatch,
):
    captured = {}
    _mock_client(
        monkeypatch,
        [
            'data: {"choices":[{"delta":{"content":"Partial summary"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
            "data: [DONE]",
        ],
        captured,
    )
    profile = ProviderConfig(
        "openai",
        "test-model",
        "https://api.example.invalid/v1/chat/completions",
        "key",
    )
    messages = [{"role": "user", "content": "Summarize"}]
    assert await _collect(stream_model(profile, messages)) == [
        "Partial summary"
    ]
    assert await _collect(
        stream_model(profile, messages, emit_finish_reason=True)
    ) == ["Partial summary", ModelFinish("openai", "length")]


def _fake_client(monkeypatch, frames, *, code=200):
    original = httpx.AsyncClient
    records = []

    def reply(request):
        records.append(request)
        return httpx.Response(
            code,
            text="\n\n".join(frames),
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(
        provider_common,
        "provider_client",
        lambda _base, **kwargs: original(
            transport=httpx.MockTransport(reply), timeout=kwargs["timeout"]
        ),
    )
    return records


@pytest.mark.asyncio
async def test_openai_content_lists_and_malformed_sse_frames(monkeypatch):
    records = _fake_client(
        monkeypatch,
        [
            ": heartbeat",
            "data: not-a-json-object",
            'data: {"choices":[{"delta":{"content":[{"text":"first"},{"text":" second"}]}}]}',
            "data: [DONE]",
            'data: {"choices":[{"delta":{"content":"ignored"}}]}',
        ],
    )
    config = ProviderConfig(
        "openai",
        "model",
        "https://mock.example.test/v1/chat/completions",
        "key",
    )
    assert await _collect(
        stream_model(
            config, [{"role": "user", "content": "hello"}], temperature=0.5
        )
    ) == ["first", " second"]
    assert json.loads(records[0].content)["temperature"] == 0.5


@pytest.mark.asyncio
async def test_multiline_anthropic_without_system_prompt(monkeypatch):
    records = _fake_client(
        monkeypatch,
        [
            'data: {"type":"content_block_delta",\ndata: "delta":{"text":"hello"}}',
        ],
    )
    config = ProviderConfig(
        "anthropic", "claude", "https://mock.example.test/v1/messages", "key"
    )
    assert await _collect(
        stream_model(
            config, [{"role": "user", "content": "hi"}], temperature=0.9
        )
    ) == ["hello"]
    assert "system" not in json.loads(records[0].content)
    assert json.loads(records[0].content)["temperature"] == 0.9


@pytest.mark.asyncio
async def test_gemini_without_system_and_nontext_part(monkeypatch):
    records = _fake_client(
        monkeypatch,
        [
            'data: {"candidates":[{"content":{"parts":[{"text":"some"},{"binary":"ignored"}]}}]}',
        ],
    )
    config = ProviderConfig(
        "google", "gemini", "https://mock.example.test/generate", "key"
    )
    assert await _collect(
        stream_model(
            config, [{"role": "user", "content": "hi"}], temperature=0.2
        )
    ) == ["some"]
    body = json.loads(records[0].content)
    assert "systemInstruction" not in body
    assert body["generationConfig"]["temperature"] == 0.2


@pytest.mark.asyncio
async def test_unexpected_provider_and_upstream_http_failure_are_bounded(
    monkeypatch,
):
    _fake_client(monkeypatch, [], code=503)
    with pytest.raises(httpx.HTTPStatusError):
        await _collect(
            stream_model(
                ProviderConfig(
                    "openai", "mock", "https://mock.example.test/v1", "secret"
                ),
                [{"role": "user", "content": "hi"}],
            )
        )
    with pytest.raises(ValueError, match="Unsupported model provider"):
        await _collect(
            stream_model(
                ProviderConfig(
                    "unsupported",
                    "mock",
                    "https://mock.example.test/v1",
                    "secret",
                ),
                [],
            )
        )


def test_explicit_profiles_resolve_all_four_protocols(tmp_path, monkeypatch):
    from workgate.hosted_chat.credential_cipher import CredentialCipher
    from workgate.hosted_chat.database import SQLiteDatabase
    from workgate.hosted_chat.provider_profiles import ProviderRepository
    from workgate.hosted_chat.providers import (
        provider_config,
        selected_provider,
    )

    db = SQLiteDatabase(tmp_path)
    repo = ProviderRepository(db, CredentialCipher(db))
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    with pytest.raises(ValueError, match="API key"):
        provider_config(repo, "openai")
    for name, kind, suffix in (
        ("Chat", "openai", "/chat/completions"),
        ("Responses", "openai-responses", "/responses"),
        ("Claude", "anthropic", "/messages"),
        (
            "Gemini channel",
            "google",
            "/models/model:streamGenerateContent?alt=sse",
        ),
    ):
        repo.save(
            {
                "name": name,
                "provider": kind,
                "base_url": "https://gateway.invalid/v1",
                "api_key": "explicit-secret",
                "models": ["model"],
                "model_sampling": {"model": {"temperature": 0.3}},
            }
        )
        config = provider_config(repo, name)
        assert config.url == "https://gateway.invalid/v1" + suffix
        assert (
            config.token == "explicit-secret"
            and "explicit-secret" not in repr(config)
        )
        assert config.channel == name and config.sampling == {
            "temperature": 0.3
        }
    repo.save(
        {
            "name": "Local",
            "provider": "openai",
            "base_url": "http://10.0.0.2:8080/v1",
            "allow_private_http": True,
            "keyless": True,
            "models": ["model"],
        }
    )
    config = provider_config(repo, "Local")
    assert config.allow_private_http and not config.token
    assert selected_provider({"endpoint": "Local"}) == "Local"
    assert (
        selected_provider({"endpoint": "agents"}, {"provider": "Chat"})
        == "Chat"
    )
    assert selected_provider({}) == "openai"
    with pytest.raises(ValueError, match="Gemini"):
        provider_config(repo, "Gemini channel", "../invalid")
    with pytest.raises(ValueError, match="model identifier"):
        provider_config(repo, "Chat", "bad\nmodel")


def _mock_frames(monkeypatch, responses):
    requests = []

    def reply(request):
        requests.append(request)
        status, events = responses.pop(0)
        content = (
            "\n\n".join(
                "data: "
                + (event if isinstance(event, str) else json.dumps(event))
                for event in events
            )
            + "\n\n"
        )
        return httpx.Response(
            status, text=content, headers={"content-type": "text/event-stream"}
        )

    monkeypatch.setattr(
        provider_common,
        "provider_client",
        lambda _base, **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(reply), timeout=kwargs["timeout"]
        ),
    )
    return requests


@pytest.mark.asyncio
async def test_openai_usage_fallback_only_before_output(monkeypatch):
    from workgate.hosted_chat.providers import ModelThought, ModelUsage

    cfg = ProviderConfig(
        "openai", "model", "https://gateway.invalid/v1/chat/completions", "key"
    )
    events = [
        {
            "choices": [
                {
                    "delta": {
                        "reasoning_content": "plan",
                        "reasoning_details": [
                            {"type": "reasoning.text", "text": "duplicate"}
                        ],
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"content": "answer"}, "finish_reason": "stop"}
            ]
        },
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}},
        "[DONE]",
    ]
    requests = _mock_frames(
        monkeypatch, [(400, ["stream_options unsupported"]), (200, events)]
    )
    assert await _collect(
        stream_model(cfg, [{"role": "user", "content": "hi"}])
    ) == [
        ModelThought("thinking", text="plan"),
        "answer",
        ModelUsage("openai", 12, 3),
    ]
    assert len(requests) == 2 and "stream_options" in json.loads(
        requests[0].content
    )
    assert "stream_options" not in json.loads(requests[1].content)
    for status, diagnostic in [
        (401, "stream_options unsupported"),
        (400, "invalid key"),
    ]:
        requests = _mock_frames(monkeypatch, [(status, [diagnostic])])
        with pytest.raises(httpx.HTTPStatusError):
            await _collect(stream_model(cfg, []))
        assert len(requests) == 1


def _openai_call(index, identity, args):
    return {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": identity,
                            "function": {"name": "read", "arguments": args},
                        }
                    ]
                }
            }
        ]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second,terminal",
    [
        (None, []),
        (None, [{"choices": [{"finish_reason": "length"}]}, "[DONE]"]),
        (_openai_call(1, "first", "{}"), ["[DONE]"]),
        (_openai_call(1, "second", "[]"), ["[DONE]"]),
        (_openai_call(1, "second", '{"x": NaN}'), ["[DONE]"]),
    ],
)
async def test_openai_never_releases_part_of_invalid_batch(
    monkeypatch, second, terminal
):
    from workgate.hosted_chat.providers import (
        ModelToolCall,
        stream_model_with_tools,
    )

    events = [_openai_call(0, "first", "{}")]
    if second is not None:
        events.append(second)
    _mock_frames(monkeypatch, [(200, events + terminal)])
    seen = []
    config = ProviderConfig(
        "openai", "model", "https://gateway.invalid/v1/chat/completions", "key"
    )
    with pytest.raises(ValueError):
        async for item in stream_model_with_tools(
            config,
            [],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        ):
            seen.append(item)
    assert not any(isinstance(item, ModelToolCall) for item in seen)


@pytest.mark.asyncio
async def test_openai_complete_parallel_tool_calls(monkeypatch):
    from workgate.hosted_chat.providers import (
        ModelToolCall,
        ModelUsage,
        stream_model_with_tools,
    )

    _mock_frames(
        monkeypatch,
        [
            (
                200,
                [
                    _openai_call(0, "first", '{"x":1}'),
                    _openai_call(1, "second", "{}"),
                    {"choices": [{"finish_reason": "tool_calls"}]},
                    {
                        "usage": {"prompt_tokens": 20, "completion_tokens": 9},
                        "choices": [],
                    },
                    "[DONE]",
                ],
            )
        ],
    )
    config = ProviderConfig(
        "openai", "model", "https://gateway.invalid/v1/chat/completions", "key"
    )
    result = await _collect(
        stream_model_with_tools(
            config,
            [],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )
    )
    assert result == [
        ModelToolCall("first", "read", {"x": 1}),
        ModelToolCall("second", "read", {}),
        ModelUsage("openai", 20, 9),
    ]


def test_confirmed_usage_and_sampling_policy():
    from workgate.hosted_chat.provider_common import (
        _anthropic_omits_sampling_parameters,
        _usage_frame,
    )
    from workgate.hosted_chat.providers import ModelUsage

    usage = _usage_frame(
        "anthropic",
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 10,
                }
            },
        },
    )
    assert usage == ModelUsage("anthropic", 130, 0)
    assert _usage_frame(
        "anthropic",
        {"type": "message_delta", "usage": {"output_tokens": 5}},
        usage,
    ) == ModelUsage("anthropic", 130, 5)
    assert _usage_frame(
        "google",
        {"usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}},
    ) == ModelUsage("google", 5, 2)
    assert (
        _usage_frame(
            "openai", {"usage": {"prompt_tokens": -1, "completion_tokens": 2}}
        )
        is None
    )
    assert _anthropic_omits_sampling_parameters("claude-opus-4-7")
    assert not _anthropic_omits_sampling_parameters("claude-sonnet-4-6")


def test_account_and_arbitrary_query_are_not_activated():
    for config in (
        ProviderConfig(
            "openai-responses",
            "model",
            "https://gateway.invalid/v1/responses",
            "",
            account_id="account",
        ),
        ProviderConfig(
            "openai",
            "model",
            "https://gateway.invalid/v1/chat/completions?secret=key",
            "key",
        ),
    ):
        with pytest.raises(ValueError):
            provider_common.provider_http_client(config)


@pytest.mark.parametrize("event", ["invalid-json", []])
async def test_openai_tool_stream_invalid_sse(monkeypatch, event):
    from workgate.hosted_chat.providers import stream_model_with_tools

    _mock_frames(monkeypatch, [(200, [event])])
    config = ProviderConfig(
        "openai", "model", "https://gateway.invalid/v1/chat/completions", "key"
    )
    with pytest.raises(ValueError, match="SSE"):
        await _collect(
            stream_model_with_tools(
                config,
                [],
                tools=[{"type": "function", "function": {"name": "read"}}],
            )
        )


async def test_openai_text_stream_options(monkeypatch):
    captured = {}
    _mock_client(
        monkeypatch,
        [
            'data: {"choices":[{"delta":{"content":[{"text":"text"}],"reasoning_content":"thought"}}]}',
            "data: [DONE]",
        ],
        captured,
    )
    config = ProviderConfig(
        "openai", "model", "https://provider.test/chat/completions", "secret"
    )
    from workgate.hosted_chat.provider_common import ModelThought

    result = await _collect(
        stream_model(config, [], max_tokens=10, temperature=0.1, top_p=0.8)
    )
    assert result == [ModelThought("thinking", text="thought"), "text"]
    body = json.loads(captured["request"].content)
    assert body["max_completion_tokens"] == 10
    assert body["temperature"] == 0.1 and body["top_p"] == 0.8
