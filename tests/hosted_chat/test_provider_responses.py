"""Final Responses protocol serialization and terminal event contracts."""

import json

import httpx
import pytest

from workgate.hosted_chat import provider_common
from workgate.hosted_chat.provider_common import provider_scope
from workgate.hosted_chat.provider_responses import responses_input
from workgate.hosted_chat.providers import (
    ModelFinish,
    ModelThought,
    ModelToolCall,
    ModelUsage,
    ProviderConfig,
    stream_model,
    stream_model_with_tools,
)

PROFILE = ProviderConfig(
    "openai-responses", "test", "https://model.invalid/v1/responses", "secret"
)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    }
]
USER = {"role": "user", "content": "Read file"}
REASON = {
    "type": "reasoning",
    "id": "rs_1",
    "summary": [],
    "encrypted_content": "encrypted-state",
}
CALL = {
    "type": "function_call",
    "id": "fc_1",
    "call_id": "call_1",
    "name": "read",
    "arguments": '{"path":"a.txt"}',
    "status": "completed",
}
MESSAGE = {
    "type": "message",
    "id": "msg_1",
    "status": "completed",
    "role": "assistant",
    "phase": "commentary",
    "content": [{"type": "output_text", "text": "Reading", "annotations": []}],
}


def complete(output, *, incomplete=False):
    return {
        "type": "response.incomplete" if incomplete else "response.completed",
        "response": {
            "status": "incomplete" if incomplete else "completed",
            "output": output,
            "usage": {"input_tokens": 100, "output_tokens": 20},
            **(
                {"incomplete_details": {"reason": "max_output_tokens"}}
                if incomplete
                else {}
            ),
        },
    }


@pytest.fixture
def transport(monkeypatch):
    batches, requests = [], []
    original = httpx.AsyncClient

    def handle(request):
        requests.append(
            (
                str(request.url),
                dict(request.headers),
                json.loads(request.content),
            )
        )
        events = batches.pop(0)
        return httpx.Response(
            200,
            text="".join(
                "data: "
                + (event if isinstance(event, str) else json.dumps(event))
                + "\n\n"
                for event in events
            ),
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(
        provider_common,
        "provider_client",
        lambda _base, **kwargs: original(
            transport=httpx.MockTransport(handle), timeout=kwargs["timeout"]
        ),
    )
    return batches, requests


@pytest.mark.asyncio
async def test_text_summary_and_terminal_fallback(transport):
    batches, requests = transport
    batches.append(
        [
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": MESSAGE,
            },
            {"type": "response.completed", "response": {"status": "completed"}},
        ]
    )
    result = [
        event
        async for event in stream_model(
            PROFILE, [USER], temperature=0.4, top_p=0.8, emit_finish_reason=True
        )
    ]
    assert result[0] == "Reading" and isinstance(result[1], ModelThought)
    assert result[-1] == ModelFinish("openai-responses", "stop")
    assert (
        requests[0][2]["temperature"] == 0.4 and requests[0][2]["top_p"] == 0.8
    )
    batches.append([complete([MESSAGE], incomplete=True)])
    result = [
        event
        async for event in stream_model(
            PROFILE, [USER], emit_finish_reason=True
        )
    ]
    assert result == [
        "Reading",
        ModelUsage("openai-responses", 100, 20),
        ModelFinish("openai-responses", "max_output_tokens"),
    ]


@pytest.mark.parametrize(
    "events,error",
    [
        (
            [
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": CALL,
                }
            ],
            RuntimeError,
        ),
        (
            [
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": CALL,
                },
                "[DONE]",
            ],
            RuntimeError,
        ),
        ([complete([CALL], incomplete=True)], RuntimeError),
        ([complete([CALL, CALL])], ValueError),
        ([complete([{**CALL, "arguments": "bad"}])], ValueError),
        ([complete([{**CALL, "arguments": "[]"}])], ValueError),
        ([complete([{**CALL, "arguments": None}])], ValueError),
        ([complete([{**CALL, "status": "in_progress"}])], RuntimeError),
        ([{"type": "error"}], RuntimeError),
        ([{"type": "response.failed"}], RuntimeError),
        ([{"type": "response.completed", "response": None}], ValueError),
        (
            [{"type": "response.completed", "response": {"output": {}}}],
            ValueError,
        ),
        (
            [
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "delta": "different",
                },
                complete([MESSAGE]),
            ],
            ValueError,
        ),
        (
            [
                {
                    "type": "response.output_text.delta",
                    "output_index": 1,
                    "delta": "missing",
                },
                complete([MESSAGE]),
            ],
            ValueError,
        ),
        (
            [
                {
                    "type": "response.output_item.done",
                    "output_index": -1,
                    "item": CALL,
                }
            ],
            ValueError,
        ),
        (
            [
                {
                    "type": "response.output_text.delta",
                    "output_index": "0",
                    "delta": "bad",
                }
            ],
            ValueError,
        ),
        (["invalid json"], ValueError),
        (["[]"], ValueError),
    ],
)
@pytest.mark.asyncio
async def test_never_dispatches_unverified_tool_calls(transport, events, error):
    transport[0].append(events)
    received = []
    with pytest.raises(error):
        async for item in stream_model_with_tools(PROFILE, [USER], tools=TOOLS):
            received.append(item)
    assert not any(isinstance(item, ModelToolCall) for item in received)


def test_multimodal_and_tool_output_serialization():
    messages = [
        {"role": "system", "content": "Instructions"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Look"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
                {
                    "type": "file",
                    "file": {
                        "filename": "a.pdf",
                        "file_data": "data:application/pdf;base64,AAAA",
                    },
                },
            ],
        },
        {
            "role": "assistant",
            "content": "Checking",
            "tool_calls": [
                {"id": "call", "function": {"name": "read", "arguments": {}}}
            ],
        },
        {"role": "tool", "tool_call_id": "call", "content": "result"},
    ]
    items = responses_input(PROFILE, messages)
    assert items[0]["role"] == "system"
    assert items[1]["content"] == [
        {"type": "input_text", "text": "Look"},
        {
            "type": "input_image",
            "image_url": "data:image/png;base64,AAAA",
            "detail": "auto",
        },
        {
            "type": "input_file",
            "filename": "a.pdf",
            "file_data": "data:application/pdf;base64,AAAA",
        },
    ]
    assert items[2] == {"role": "assistant", "content": "Checking"}
    assert items[3]["arguments"] == "{}" and items[4]["output"] == "result"


@pytest.mark.parametrize(
    "message",
    [
        {"role": "user", "content": 3},
        {"role": "user", "content": ["invalid"]},
        {"role": "user", "content": [{"type": "image_url", "image_url": {}}]},
        {"role": "user", "content": [{"type": "file", "file": None}]},
        {"role": "user", "content": [{"type": "file", "file": {}}]},
        {"role": "user", "content": [{"type": "unknown"}]},
        {"role": "tool", "content": "missing identity"},
        {"role": "unknown", "content": "invalid"},
    ],
)
def test_invalid_input_rejected(message):
    with pytest.raises(ValueError):
        responses_input(PROFILE, [message])


def test_structured_tool_output_and_empty_messages():
    items = responses_input(
        PROFILE,
        [
            {"role": "user", "content": ""},
            {"role": "developer", "content": "Rules"},
            {
                "role": "tool",
                "tool_call_id": "call",
                "content": [{"type": "text", "text": "result"}],
            },
        ],
    )
    assert items == [
        {
            "role": "developer",
            "content": [{"type": "input_text", "text": "Rules"}],
        },
        {
            "type": "function_call_output",
            "call_id": "call",
            "output": [{"type": "input_text", "text": "result"}],
        },
    ]


@pytest.mark.parametrize(
    "extra",
    [
        {"index": -1, "item": REASON},
        {"index": 0, "item": None},
        {"index": 0, "item": {"type": "unknown"}},
        {"index": 0, "item": {"type": "function_call"}},
        {"index": 0, "item": {"type": "function_call", "call_id": "another"}},
        {"index": 1, "item": MESSAGE},
    ],
)
def test_invalid_replay_metadata_falls_back(extra):
    scope = provider_scope(PROFILE)
    meta = {"type": "responses_item", "scope": scope}
    message = {
        "role": "assistant",
        "content": "Reading",
        "reasoning": [{**meta, "index": 1, "item": MESSAGE}, {**meta, **extra}],
    }
    assert responses_input(PROFILE, [message]) == [
        {"role": "assistant", "content": "Reading"}
    ]


def test_replay_ignores_unencrypted_reasoning_and_checks_call_metadata():
    scope = provider_scope(PROFILE)
    meta = {"type": "responses_item", "scope": scope}
    message = {
        "role": "assistant",
        "content": "Reading",
        "reasoning": [
            {**meta, "index": 0, "item": {"type": "reasoning"}},
            {**meta, "index": 1, "item": MESSAGE},
        ],
    }
    assert responses_input(PROFILE, [message]) == [MESSAGE]
    message["tool_calls"] = [
        {"id": "call", "function": {"name": "read", "arguments": "{}"}}
    ]
    assert responses_input(PROFILE, [message]) == [
        {"role": "assistant", "content": "Reading"},
        {
            "type": "function_call",
            "call_id": "call",
            "name": "read",
            "arguments": "{}",
        },
    ]
    message["reasoning"] = [
        {
            **meta,
            "index": 0,
            "item": {"type": "function_call", "call_id": "call"},
        },
        {**meta, "index": 0, "item": MESSAGE},
    ]
    assert responses_input(PROFILE, [message])[0] == {
        "role": "assistant",
        "content": "Reading",
    }


@pytest.mark.asyncio
async def test_refusal_keyless_stream_and_empty_deltas(transport):
    batches, requests = transport
    refusal = {
        **MESSAGE,
        "content": [{"type": "refusal", "refusal": "Cannot comply"}],
    }
    batches.append(
        [
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "delta": "",
            },
            {"type": "response.reasoning_summary_text.delta", "delta": ""},
            {
                "type": "response.refusal.delta",
                "output_index": 0,
                "delta": "Cannot comply",
            },
            complete(
                [refusal, {"type": "reasoning", "id": "rs_2", "summary": []}]
            ),
        ]
    )
    keyless = ProviderConfig(PROFILE.provider, PROFILE.model, PROFILE.url, "")
    result = [item async for item in stream_model(keyless, [USER])]
    assert result[0] == "Cannot comply"
    assert "authorization" not in requests[0][1]
    assert len([item for item in result if isinstance(item, ModelThought)]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events,error",
    [
        ([complete([MESSAGE], incomplete=True)], RuntimeError),
        ([complete([{"type": "unknown"}])], ValueError),
        ([complete([None])], ValueError),
        (
            [{"type": "response.completed", "response": {"status": "failed"}}],
            RuntimeError,
        ),
        ([complete([CALL])], ValueError),
    ],
)
async def test_text_stream_terminal_errors(transport, events, error):
    transport[0].append(events)
    with pytest.raises(error):
        _ = [item async for item in stream_model(PROFILE, [USER])]


@pytest.mark.asyncio
async def test_unsupported_tool_declaration():
    with pytest.raises(ValueError, match="Unsupported Responses tool"):
        _ = [
            item
            async for item in stream_model_with_tools(
                PROFILE, [USER], tools=[{"type": "invalid"}]
            )
        ]


@pytest.mark.asyncio
async def test_terminal_replay_scoped_to_exact_channel(transport):
    from dataclasses import replace

    batches, requests = transport
    cfg = replace(PROFILE, channel="original")
    batches.append([complete([REASON, MESSAGE, CALL])])
    received = [
        event
        async for event in stream_model_with_tools(cfg, [USER], tools=TOOLS)
    ]
    assert received[0] == "Reading"
    calls = [event for event in received if isinstance(event, ModelToolCall)]
    assert len(calls) == 1
    call = calls[0]
    reasoning = [
        {"type": event.kind, **event.provider_data}
        for event in received
        if isinstance(event, ModelThought)
        and event.kind == "responses_item"
        and event.provider_data
    ]
    history = [
        USER,
        {
            "role": "assistant",
            "content": "Reading",
            "reasoning": reasoning,
            "tool_calls": [
                {
                    "id": call.call_id,
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                    **call.history_metadata(),
                }
            ],
        },
    ]
    wire = responses_input(cfg, history)
    assert wire[1] == REASON and wire[2] == MESSAGE
    assert wire[3]["call_id"] == call.call_id
    for other in (
        replace(cfg, channel="other"),
        replace(cfg, token="other"),
        replace(cfg, model="other"),
    ):
        assert "encrypted-state" not in json.dumps(
            responses_input(other, history)
        )
    assert requests[0][2]["store"] is False
