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
