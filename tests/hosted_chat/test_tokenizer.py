"""Fixed Node o200k-base and exact original TypeScript chunk-loop vectors."""

import json
from pathlib import Path

import pytest

from workgate.hosted_chat import tokenizer
from workgate.hosted_chat.tokenizer import count_tokens

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures/tokenizer_vectors.json").read_text(
        encoding="utf-8"
    )
)["vectors"]


@pytest.mark.parametrize("index", range(len(VECTORS)))
def test_js_o200k_base_token_parity(index):
    pytest.importorskip("tiktoken")
    case = VECTORS[index]
    assert count_tokens(case["text"]) == case["exact"]
    assert count_tokens(case["text"], exact=False) == case["bounded"]


def test_missing_tokenizer_falls_back_to_utf8_bytes(monkeypatch):
    def unavailable():
        raise ImportError("optional dependency unavailable")

    monkeypatch.setattr(tokenizer, "_encoding", unavailable)
    for case in VECTORS:
        text = case["text"]
        expected = len(text.encode("utf-8", "surrogatepass"))
        assert count_tokens(text) == expected
        assert count_tokens(text, exact=False) == expected
        assert expected >= case["exact"]


def test_corrupt_rank_data_is_rejected(monkeypatch):
    pytest.importorskip("tiktoken")
    tokenizer._encoding.cache_clear()
    monkeypatch.setattr(tokenizer, "_HASH", "bad")
    try:
        with pytest.raises(ValueError, match="integrity validation"):
            count_tokens("hello")
    finally:
        tokenizer._encoding.cache_clear()
