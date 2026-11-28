"""Provider-only fading projection regressions."""

import copy
import json

from workgate.hosted_chat.fading_projection import (
    project_faded_provider_messages,
)


def _history():
    return [
        {"role": "user", "content": "Read a very large file."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "large-call",
                    "type": "function",
                    "function": {
                        "name": "file_read",
                        "arguments": json.dumps({"path": "/tmp/" + "x" * 5000}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "large-call",
            "content": "large data " * 7000,
        },
        {"role": "assistant", "content": "I read the tool output."},
        {"role": "user", "content": "Summarize the contents."},
    ]


def test_provider_view_truncation_preserves_full_source_and_pairing():
    original = _history()
    unchanged = copy.deepcopy(original)
    tier = {"v": 2, "budgetTokens": 170, "masked": True, "latched": True}
    projected = project_faded_provider_messages(original, tier)
    assert original == unchanged
    assert projected != original
    assert len(projected[2]["content"]) <= 300
    assert projected[2]["tool_call_id"] == "large-call"
    arguments = json.loads(
        projected[1]["tool_calls"][0]["function"]["arguments"]
    )
    assert arguments["_originalChars"] > 5000
    assert len(projected[1]["tool_calls"][0]["function"]["arguments"]) <= 100
    assert original[2]["content"].endswith("large data ")
    assert project_faded_provider_messages(original, None) is original
    assert (
        project_faded_provider_messages(
            original, {"v": 1, "budgetTokens": 1000, "masked": True}
        )
        is original
    )


def test_fading_provider_projection_never_uses_invalid_truncated_json():
    tiny = {"v": 2, "budgetTokens": 1, "masked": True}
    source = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "a", "function": {"arguments": "abcde" * 1000}}
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "long" * 5000},
    ]
    messages = project_faded_provider_messages(source, tiny)
    assert (
        json.loads(messages[0]["tool_calls"][0]["function"]["arguments"]) == {}
    )
    assert len(messages[1]["content"]) <= 1
    assert source[0]["tool_calls"][0]["function"]["arguments"] == "abcde" * 1000
    assert source[1]["content"] == "long" * 5000


def test_fading_projection_handles_non_dict_parts_and_malformed_tool_declarations():
    payload = [
        None,
        1,
        {
            "role": "assistant",
            "tool_calls": [
                None,
                {"id": "missing"},
                {"id": "badargs", "function": {"arguments": 1000}},
                {"id": "short", "function": {"arguments": "{}"}},
            ],
        },
        {"role": "tool", "content": "short"},
        {"role": "user", "content": "hello"},
    ]
    tier = {"v": 2, "budgetTokens": 256, "masked": False}
    projected = project_faded_provider_messages(payload, tier)
    assert projected == payload and projected is not payload
    assert projected[0] is None and projected[1] == 1
    assert projected[3]["content"] == "short"


def test_fading_projection_calibrated_provider_budget_and_escaped_json_arguments():
    original = [
        {
            "role": "assistant",
            "tool_calls": [{"function": {"arguments": "\\" * 5000}}],
        },
        {"role": "tool", "content": "Z" * 5000},
        None,
        {"role": "assistant", "content": None},
        {"role": "assistant", "content": "I completed the tool read."},
    ]
    rendered = project_faded_provider_messages(
        original,
        {"v": 2, "budgetTokens": 2000, "masked": True},
        max_tool_result_chars=350,
    )
    params = rendered[0]["tool_calls"][0]["function"]["arguments"]
    assert len(params) <= 1200
    assert json.loads(params)["_originalChars"] == 5000
    assert len(rendered[1]["content"]) <= 350
    assert original[1]["content"] == "Z" * 5000
    assert rendered[2] is None


def test_clip_bounded_head_and_tail_always_respect_cap():
    from workgate.hosted_chat.fading_projection import _clip

    assert _clip("abc", 12) == "abc"
    assert _clip("abcdef" * 30, 15).endswith("…")
    assert len(_clip("z" * 500, 40)) <= 40
    assert len(_clip("z" * 500, 5)) == 5
    assert _clip("z" * 500, 40) != "z" * 500
