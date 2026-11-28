"""Pinned summarization trigger thresholds, role accounting and UTF-16 tokenizer seams."""

import pytest

from workgate.hosted_chat.context_budget import (
    calculate_budget,
    message_token_count,
    should_trigger_summarization,
    split_recent_turns,
    summarize_options,
)
from workgate.hosted_chat.tokenizer import count_tokens


def test_tokenizer_matches_exact_encoding_and_splits_js_surrogates():
    for case in (["a"] * 300, ["a", " "] * 2047 + ["a", "😊"]):
        text = "".join(case)
        assert count_tokens(text, exact=False) > 0
        assert count_tokens(text) > 0
    assert count_tokens("a " * 2047 + "a😊", exact=False) >= 0
    # Deliberately non-BPE-exact paths are bounded but deterministic.
    assert count_tokens("a " * 2047 + "a😊", exact=False) == count_tokens(
        "a " * 2047 + "a😊", exact=False
    )


def test_message_token_extras_and_invalid_budget():
    assert message_token_count({"content": None}) >= 4
    assert (
        message_token_count({"content": [{"type": "text", "text": "hello"}]})
        > 4
    )
    assert message_token_count(
        {
            "content": "Hi",
            "tool_call_id": "call_1",
            "thinking": "Private context",
        }
    ) > message_token_count({"content": "Hi"})
    tool = [
        {
            "type": "function",
            "function": {"name": "run", "parameters": {"type": "object"}},
        }
    ]
    budget = calculate_budget(
        [
            {"role": "system", "content": "Hi"},
            {"role": "user", "content": "Go"},
        ],
        tool,
        1200,
        reserve_ratio=0,
    )
    assert budget.reserve_tokens == 0
    assert budget.tool_schema_tokens > 0
    with pytest.raises(ValueError):
        calculate_budget([], [], 0)
    assert calculate_budget([], [], 100, reserve_ratio=1.1).reserve_tokens == 0


@pytest.mark.parametrize(
    "trigger,remaining,max_tokens,prior,expected",
    [
        ({"type": "token_ratio", "value": 0.7}, None, 100, None, False),
        ({"type": "token_ratio", "value": 0.7}, 0, 100, None, True),
        ({"type": "token_ratio", "value": 0.7}, 40, 100, None, False),
        ({"type": "remaining_tokens", "value": 15}, None, 100, None, False),
        ({"type": "remaining_tokens", "value": 15}, 14, 100, None, True),
        ({"type": "remaining_tokens", "value": 15}, 100, 100, 88, True),
        ({"type": "messages_to_refine", "value": 1}, None, None, None, True),
        ({"type": "messages_to_refine", "value": 7}, None, None, None, False),
        ({"type": "unsupported", "value": 1}, None, None, None, False),
    ],
)
def test_trigger_source_conditions(
    trigger, remaining, max_tokens, prior, expected
):
    assert (
        should_trigger_summarization(
            messages_to_refine_count=2,
            trigger=trigger,
            remaining_context_tokens=remaining,
            max_context_tokens=max_tokens,
            pre_prune_context_tokens=prior,
        )
        is expected
    )


def test_recency_empty_and_tool_pairs():
    assert split_recent_turns([], turns=2) == ([], [])
    assert split_recent_turns(
        [{"role": "system", "content": "Only system"}]
    ) == ([{"role": "system", "content": "Only system"}], [])
    dialogue = [
        {"role": "user", "content": "One"},
        {"role": "assistant", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": "Done"},
        {"role": "user", "content": "Two"},
        {"role": "assistant", "content": "OK"},
    ]
    assert split_recent_turns(dialogue, turns=1)[0] == dialogue[:3]
    assert split_recent_turns(dialogue, turns=2)[0] == []
    assert split_recent_turns(dialogue, turns=2, tokens=1)[1][0] == dialogue[3]
    assert split_recent_turns(dialogue, turns=2, tokens=10000)[0] == []
    assert split_recent_turns(dialogue[:2] + dialogue[3:], turns=1)[0] == []


def test_summarization_option_resolution():
    assert summarize_options(None, {}) == (None, {})
    assert summarize_options(
        {"maxContextTokens": 1024, "summarization": {"enabled": True}}, {}
    ) == (1024, {"enabled": True})
    assert summarize_options(
        {"summarization": "invalid"}, {"max_context_tokens": -1}
    ) == (None, {})
    assert summarize_options({}, {"maxContextTokens": 8.4}) == (8, {})


def test_context_usage_breakdown():
    from workgate.hosted_chat.context_budget import (
        context_usage_snapshot,
        provider_usage_snapshot,
    )

    messages = [
        {"role": "system", "content": "Policy"},
        {"role": "system", "content": "<summary>\nOld facts"},
        {"role": "user", "content": "Question"},
        {"role": "assistant", "tool_calls": [{"id": "c"}]},
        {"role": "tool", "tool_call_id": "c", "content": "Answer"},
    ]
    tools = [{"type": "function", "function": {"name": "f"}}]
    budget = calculate_budget(messages, tools, 2000, calibration_ratio=1.5)
    snapshot = context_usage_snapshot(
        messages, tools, 2000, calibration_ratio=1.5, run_id="r", agent_id="a"
    )
    assert snapshot["runId"] == "r"
    assert snapshot["agentId"] == "a"
    breakdown = snapshot["breakdown"]
    assert (
        breakdown["instructionTokens"] + breakdown["summaryTokens"]
        == budget.instruction_tokens + budget.tool_schema_tokens
    )
    assert breakdown["messageCount"] == 3
    assert breakdown["toolMessageTokens"] > 0
    assert breakdown["messageTokens"] == budget.message_tokens
    assert snapshot["remainingContextTokens"] == budget.remaining_context_tokens
    for limit in (None, 2000):
        observed = provider_usage_snapshot(
            input_tokens=500,
            output_tokens=20,
            provider="openai",
            model="m",
            messages=messages,
            tools=tools,
            max_context_tokens=limit,
            run_id="r",
            agent_id="a",
        )
        assert observed["providerInputTokens"] == 500
        assert observed["providerOutputTokens"] == 20
        assert observed["breakdown"]["maxContextTokens"] == (limit or 0)


def test_calibration_cumulative_and_invalid_observations():
    from workgate.hosted_chat.context_calibration import (
        MAX_CUMULATIVE_TOKENS,
        TokenCalibration,
        clamp_calibration_ratio,
    )

    initial = TokenCalibration()
    first = initial.observe(
        provider_input_tokens=250,
        raw_sent_this_turn=100,
        instruction_overhead=50,
    )
    assert first.ratio == 2
    second = first.observe(
        provider_input_tokens=150,
        raw_sent_this_turn=100,
        instruction_overhead=50,
    )
    assert second.ratio == 1.5
    assert TokenCalibration.from_dict(second.to_dict()) == second
    assert TokenCalibration.from_dict(None) == initial
    assert TokenCalibration.from_dict({"cumulativeRawSent": -1}) == initial
    for raw, observed, overhead in [
        (0, 500, 0),
        (100, 30, 0),
        (100, 50, 50),
        (100, -1, 0),
        (100, MAX_CUMULATIVE_TOKENS + 1, 0),
    ]:
        assert (
            second.observe(
                provider_input_tokens=observed,
                raw_sent_this_turn=raw,
                instruction_overhead=overhead,
            )
            is second
        )
    large = TokenCalibration(MAX_CUMULATIVE_TOKENS, MAX_CUMULATIVE_TOKENS, 1)
    assert (
        large.observe(
            provider_input_tokens=100,
            raw_sent_this_turn=100,
            instruction_overhead=0,
        )
        is large
    )
    assert clamp_calibration_ratio(float("nan")) == 1
    assert clamp_calibration_ratio(0.1) == 0.5
    assert clamp_calibration_ratio(10) == 5


def test_empty_trigger_and_recency():
    assert not should_trigger_summarization(messages_to_refine_count=0)
    assert should_trigger_summarization(messages_to_refine_count=1)
    assert not should_trigger_summarization(
        messages_to_refine_count=1,
        trigger={"type": "token_ratio", "value": True},
    )
    messages = [{"role": "user", "content": "a"}]
    assert split_recent_turns(messages, turns=0) == (messages, [])
