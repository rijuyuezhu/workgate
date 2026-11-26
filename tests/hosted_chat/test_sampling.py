"""Model defaults and ordinary-chat sampling must reach the native generation path."""

import pytest

from workgate.hosted_chat.sampling import (
    merge_sampling,
    validate_channel_sampling,
    validate_sampling,
)


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"other": 1},
        {"temperature": True},
        {"temperature": -1},
        {"temperature": 2.1},
        {"temperature": float("nan")},
        {"top_p": 1.1},
        {"max_tokens": 0},
        {"max_tokens": 1.5},
        {"max_tokens": 2_000_001},
    ],
)
def test_invalid_sampling_rejected(value):
    with pytest.raises(ValueError):
        validate_sampling(value)


def test_anthropic_sampling_temperature_limit():
    assert validate_sampling({"temperature": 1}, provider="anthropic") == {
        "temperature": 1
    }
    with pytest.raises(ValueError, match="temperature"):
        validate_sampling({"temperature": 1.5}, provider="anthropic")
    assert validate_sampling({"temperature": 1.5}, provider="openai") == {
        "temperature": 1.5
    }


def test_sampling_inheritance_and_legacy_agent_aliases():
    defaults = {"temperature": 0.7, "top_p": 0.9, "max_tokens": 256}
    assert validate_sampling(
        {"temperature": 0, "top_p": 0, "max_tokens": 1}
    ) == {"temperature": 0, "top_p": 0, "max_tokens": 1}
    assert merge_sampling(
        defaults,
        {
            "temperature": 0,
            "topP": 0.5,
            "maxOutputTokens": 100,
            "stop": ["END"],
        },
    ) == {"temperature": 0, "top_p": 0.5, "max_tokens": 100, "stop": ["END"]}
    assert merge_sampling(defaults, {}) == defaults


def test_channel_sampling_uses_selected_protocol():
    def profile(channel):
        assert channel == "channel"
        return {"provider": "anthropic"}

    assert validate_channel_sampling(
        {"temperature": 0}, "channel", profile=profile
    ) == {"temperature": 0}
    with pytest.raises(ValueError, match="temperature"):
        validate_channel_sampling(
            {"temperature": 1.5}, "channel", profile=profile
        )
