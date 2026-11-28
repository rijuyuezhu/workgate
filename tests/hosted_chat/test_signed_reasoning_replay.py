"""Opaque provider reasoning is replayed only to its originating connection."""

from dataclasses import replace

import pytest

from workgate.hosted_chat.agent_runtime import (
    AgentContext,
    AgentLoopState,
    ToolBatchOutput,
    run_agent_loop,
)
from workgate.hosted_chat.provider_common import (
    ModelThought,
    ModelToolCall,
    ProviderConfig,
    provider_scope,
)
from workgate.hosted_chat.provider_tool_protocols import native_messages


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["same", "missing", "mixed", "foreign"])
async def test_signed_thinking_scope_and_fragments(mode):
    profile = ProviderConfig(
        "anthropic",
        "claude",
        "https://model.invalid",
        "test-secret",
        channel="primary",
    )
    scope = provider_scope(profile)
    state = AgentLoopState(
        AgentContext(profile, [{"name": "file_read"}]),
        [{"role": "user", "content": "question"}],
    )
    scopes = {"scope": scope}
    first_scope = None if mode == "missing" else scopes
    second_scope = (
        {"scope": "foreign"} if mode in {"mixed", "foreign"} else first_scope
    )
    if mode == "foreign":
        first_scope = second_scope
    calls = []
    output = []

    async def stream(model, messages, **kwargs):
        calls.append(messages.copy())
        if len(calls) == 1:
            yield ModelThought("thinking", text="reasoning")
            yield ModelThought(
                "signature", signature="part1", provider_data=first_scope
            )
            yield ModelThought(
                "signature", signature="part2", provider_data=second_scope
            )
            yield ModelThought(
                "redacted_thinking", data="opaque", provider_data=first_scope
            )
            yield ModelToolCall("read", "file_read", {"path": "test"})
        else:
            yield "done"

    async def stream_text(*args, **kwargs):
        pytest.fail("Unexpected non-tool stream")
        yield ""

    async def dispatch(calls, current):
        return ToolBatchOutput(
            [{"role": "tool", "tool_call_id": "read", "content": "result"}]
        )

    async def on_text(text):
        output.append(text)

    await run_agent_loop(
        state,
        stream_tools=stream,
        stream_text=stream_text,
        dispatch=dispatch,
        on_text=on_text,
    )
    first = state.messages[1]
    signed, redacted = first["reasoning"]
    assert signed["signature"] == "part1part2"
    assert signed.get("scope") == (scope if mode == "same" else None)
    assert redacted.get("scope") == (
        scope if mode in {"same", "mixed"} else None
    )
    assert output == ["done"]
    projected, _ = native_messages(calls[1], "anthropic", scope=scope)
    expected = []
    if mode == "same":
        expected.append(
            {
                "type": "thinking",
                "thinking": "reasoning",
                "signature": "part1part2",
            }
        )
    if mode in {"same", "mixed"}:
        expected.append({"type": "redacted_thinking", "data": "opaque"})
    assert projected[1]["content"][:-1] == expected
    assert projected[1]["content"][-1]["type"] == "tool_use"
    for changed in (
        replace(profile, token="changed"),
        replace(profile, model="another"),
        replace(profile, channel="secondary"),
        replace(profile, url="https://another.invalid"),
    ):
        projected, _ = native_messages(
            state.messages, "anthropic", scope=provider_scope(changed)
        )
        assert projected[1]["content"] == [
            {
                "type": "tool_use",
                "id": "read",
                "name": "file_read",
                "input": {"path": "test"},
            }
        ]
        assert "part1part2" not in str(projected)
        assert "opaque" not in str(projected)
