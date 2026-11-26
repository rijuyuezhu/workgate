"""Native provider wire protocols with real httpx SSE parsing and continuation."""

import json

import httpx
import pytest

from workgate.hosted_chat import provider_common
from workgate.hosted_chat.provider_tool_protocols import native_messages
from workgate.hosted_chat.providers import (
    ModelThought,
    ModelToolCall,
    ProviderConfig,
    stream_model_with_tools,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "Read file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    }
]


@pytest.fixture
def transport(monkeypatch):
    events = []
    requests = []
    original = httpx.AsyncClient

    def handle(request):
        requests.append((dict(request.headers), json.loads(request.content)))
        content = "".join(
            "data: "
            + (event if isinstance(event, str) else json.dumps(event))
            + "\n\n"
            for event in events
        )
        return httpx.Response(
            200, text=content, headers={"content-type": "text/event-stream"}
        )

    mock = httpx.MockTransport(handle)
    monkeypatch.setattr(
        provider_common,
        "provider_client",
        lambda _base, **kwargs: original(
            transport=mock, timeout=kwargs["timeout"]
        ),
    )
    return events, requests


async def collect(provider, events, *, messages=None, temperature=None):
    return [
        event
        async for event in stream_model_with_tools(
            ProviderConfig(
                provider, "test", "https://provider.test/stream", "secret"
            ),
            messages
            or [
                {"role": "system", "content": "Instructions"},
                {"role": "user", "content": "Read a file"},
            ],
            tools=TOOLS,
            temperature=temperature,
        )
    ]


@pytest.mark.parametrize("provider", ["anthropic", "google"])
async def test_native_tool_roundtrip_preserves_protocol_and_credentials(
    transport, provider
):
    events, requests = transport
    if provider == "anthropic":
        events.extend(
            [
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Checking"},
                },
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "file_read",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"path":',
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '"a.txt"}',
                    },
                },
                {"type": "content_block_stop", "index": 1},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
                {"type": "message_stop"},
            ]
        )
    else:
        events.extend(
            [
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": "private thought",
                                        "thought": True,
                                    },
                                    {"text": "Checking"},
                                    {
                                        "functionCall": {
                                            "name": "file_read",
                                            "args": {"path": "a.txt"},
                                        },
                                        "thoughtSignature": "signature",
                                    },
                                ]
                            }
                        }
                    ]
                },
                "[DONE]",
            ]
        )
    result = await collect(provider, events, temperature=0.3)
    if provider == "google":
        assert result[0] == ModelThought("thinking", text="private thought")
        result = result[1:]
    assert result[0] == "Checking"
    call = result[1]
    assert isinstance(call, ModelToolCall)
    assert call.name == "file_read" and call.arguments == {"path": "a.txt"}
    headers, body = requests[-1]
    assert "secret" not in json.dumps(body)
    if provider == "anthropic":
        assert headers["x-api-key"] == "secret"
        assert (
            body["tools"][0]["input_schema"]
            == TOOLS[0]["function"]["parameters"]
        )
    else:
        assert headers["x-goog-api-key"] == "secret"
        assert (
            body["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"]
            == TOOLS[0]["function"]["parameters"]
        )
    history = [
        {"role": "system", "content": "Instructions"},
        {"role": "user", "content": "Read file"},
        {
            "role": "assistant",
            "content": "Checking",
            "tool_calls": [
                {
                    "id": call.call_id,
                    "type": "function",
                    **call.history_metadata(),
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call.call_id,
            "content": "File contents",
        },
    ]
    events[:] = (
        [
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Done"},
            }
        ]
        if provider == "anthropic"
        else [{"candidates": [{"content": {"parts": [{"text": "Done"}]}}]}]
    )
    assert await collect(provider, events, messages=history) == ["Done"]
    body = requests[-1][1]
    if provider == "anthropic":
        assert body["messages"][-1]["content"][0]["tool_use_id"] == call.call_id
    else:
        assert (
            body["contents"][-2]["parts"][-1]["thoughtSignature"] == "signature"
        )
        assert (
            body["contents"][-1]["parts"][0]["functionResponse"]["name"]
            == "file_read"
        )


@pytest.mark.parametrize("provider", ["anthropic", "google"])
async def test_unknown_frames_and_provider_error(transport, provider):
    events, _ = transport
    events[:] = [
        {"unrecognized": True},
        {"error": {"message": "private provider diagnostic"}},
    ]
    with pytest.raises(RuntimeError, match="rejected"):
        await collect(provider, events)


@pytest.mark.parametrize(
    "case",
    ["index", "undeclared", "oversized", "arguments", "incomplete", "many"],
)
async def test_anthropic_rejects_invalid_partial_tool_calls(transport, case):
    events, _ = transport
    start = {
        "type": "content_block_start",
        "index": 0,
        "content_block": {
            "type": "tool_use",
            "id": "id",
            "name": "file_read",
            "input": {},
        },
    }
    delta = {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": "{}"},
    }
    if case == "index":
        start["index"] = -1
        events[:] = [start]
    elif case == "undeclared":
        events[:] = [delta]
    elif case == "oversized":
        delta["delta"]["partial_json"] = "x" * 9000
        events[:] = [start, delta]
    elif case == "arguments":
        delta["delta"]["partial_json"] = "[]"
        events[:] = [start, delta]
    elif case == "incomplete":
        start["content_block"]["name"] = ""
        events[:] = [start]
    else:
        events[:] = (
            [{**start, "index": index} for index in range(5)]
            + [
                {"type": "content_block_stop", "index": index}
                for index in range(5)
            ]
            + [
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
                {"type": "message_stop"},
            ]
        )
        with pytest.raises(ValueError, match="duplicate"):
            await collect("anthropic", events)
        return
    with pytest.raises(ValueError):
        await collect("anthropic", events)


@pytest.mark.parametrize(
    "function",
    [[], {"name": "file_read", "args": []}, {"name": "", "args": {}}],
)
async def test_gemini_rejects_invalid_tool_calls(transport, function):
    events, _ = transport
    events[:] = [
        {"candidates": [{"content": {"parts": [{"functionCall": function}]}}]}
    ]
    with pytest.raises(ValueError):
        await collect("google", events)


async def test_gemini_parallel_calls_and_empty_history(transport):
    events, _ = transport
    events[:] = [
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"functionCall": {"name": "file_read"}}
                            for _ in range(5)
                        ]
                    },
                    "finishReason": "STOP",
                }
            ]
        }
    ]
    assert len(await collect("google", events)) == 5
    assert native_messages(
        [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}],
        "google",
    ) == ([{"role": "user", "parts": [{"text": "a"}, {"text": "b"}]}], "")


def test_opaque_native_replay_is_bound_to_connection():
    from dataclasses import replace

    from workgate.hosted_chat.provider_common import provider_scope

    config = ProviderConfig(
        "anthropic",
        "model",
        "https://gateway.invalid/v1/messages",
        "secret",
        channel="first",
    )
    scope = provider_scope(config)
    history = [
        {
            "role": "assistant",
            "content": "answer",
            "reasoning": [
                {
                    "type": "thinking",
                    "thinking": "provider thought",
                    "signature": "signed",
                    "scope": scope,
                },
                {"type": "redacted_thinking", "data": "opaque", "scope": scope},
            ],
        }
    ]
    projected, _ = native_messages(history, "anthropic", scope=scope)
    assert projected[0]["content"][0]["signature"] == "signed"
    assert projected[0]["content"][1]["data"] == "opaque"
    projected, _ = native_messages(
        history,
        "anthropic",
        scope=provider_scope(replace(config, channel="second")),
    )
    assert projected[0]["content"] == "answer"


@pytest.mark.parametrize("provider", ["anthropic", "google"])
async def test_native_tool_stream_disconnect_does_not_release_call(
    transport, provider
):
    events, _ = transport
    if provider == "anthropic":
        events[:] = [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "id",
                    "name": "file_read",
                    "input": {},
                },
            }
        ]
    else:
        events[:] = [
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "file_read",
                                        "args": {},
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    with pytest.raises(ValueError, match="complete tool batch"):
        await collect(provider, events)


@pytest.mark.parametrize("provider", ["anthropic", "google"])
def test_native_projection_media_and_tool_error(provider):
    history = [
        {"role": "system", "content": [{"type": "text", "text": "policy"}]},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "inspect"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,YQ=="},
                },
                {
                    "type": "file",
                    "file": {"file_data": "data:application/pdf;base64,YQ=="},
                },
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "a", "function": {"name": "file_read", "arguments": {}}},
                {
                    "id": "b",
                    "function": {"name": "file_read", "arguments": "{}"},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "a",
            "content": [{"type": "text", "text": "failed"}],
            "is_error": True,
        },
        {"role": "tool", "tool_call_id": "b", "content": "ok"},
        {"role": "user", "content": "continue"},
    ]
    wire, system = native_messages(history, provider)
    assert system == "policy"
    data = json.dumps(wire)
    assert "YQ==" in data and "application/pdf" in data
    assert '"is_error": true' in data and "failed" in data
    assert "continue" in data


@pytest.mark.parametrize(
    "provider,message",
    [
        ("unsupported", {"role": "user", "content": "hi"}),
        ("anthropic", {"role": "system", "content": 3}),
        ("google", {"role": "unknown", "content": "hi"}),
        ("anthropic", {"role": "user", "content": 3}),
        ("google", {"role": "user", "content": [3]}),
        ("anthropic", {"role": "user", "content": [{"type": "audio"}]}),
        (
            "google",
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://remote.invalid/x"},
                    }
                ],
            },
        ),
        ("anthropic", {"role": "tool", "content": "hi"}),
        (
            "google",
            {"role": "tool", "tool_call_id": "missing", "content": "hi"},
        ),
        ("anthropic", {"role": "tool", "tool_call_id": "a", "content": 3}),
        (
            "google",
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "a",
                        "function": {"name": "file_read", "arguments": "[]"},
                    }
                ],
            },
        ),
    ],
)
def test_native_projection_rejects_invalid_messages(provider, message):
    with pytest.raises(ValueError):
        native_messages([message], provider)


@pytest.mark.parametrize("event", ["invalid-json", []])
async def test_native_rejects_invalid_sse(transport, event):
    events, _ = transport
    events[:] = [event]
    with pytest.raises(ValueError, match="SSE"):
        await collect("anthropic", events)


async def test_anthropic_thoughts_usage_and_finish(transport):
    from workgate.hosted_chat.provider_common import ModelFinish, ModelUsage
    from workgate.hosted_chat.provider_tool_protocols import stream_native_tools

    events, _ = transport
    events[:] = [
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 8, "output_tokens": 0}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "redacted_thinking", "data": "opaque"},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "thinking_delta", "thinking": "thought"},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "signature_delta", "signature": "signed"},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    ]
    config = ProviderConfig(
        "anthropic", "model", "https://provider.test/messages", "secret"
    )
    result = [
        x
        async for x in stream_native_tools(
            config,
            [],
            tools=TOOLS,
            max_tokens=10,
            temperature=0.3,
            top_p=0.8,
            emit_finish_reason=True,
        )
    ]
    assert isinstance(result[0], ModelThought) and result[0].data == "opaque"
    assert result[1] == ModelThought("thinking", text="thought")
    assert (
        isinstance(result[2], ModelThought) and result[2].signature == "signed"
    )
    assert result[-2:] == [
        ModelUsage("anthropic", 8, 3),
        ModelFinish("anthropic", "end_turn"),
    ]


@pytest.mark.parametrize(
    "tail",
    [
        [
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ],
        [
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        ],
        [
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": "{}"},
            },
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ],
    ],
)
async def test_anthropic_incomplete_or_late_tool_block(transport, tail):
    events, _ = transport
    events[:] = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "id",
                "name": "file_read",
                "input": {},
            },
        },
        *tail,
    ]
    with pytest.raises(ValueError):
        await collect("anthropic", events)
