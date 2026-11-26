"""Pinned v6.0.3 provider profile discovery cases."""

import httpx
import pytest

from workgate.hosted_chat import model_discovery as discovery
from workgate.hosted_chat.model_discovery import _fetch_models


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["anthropic", "google"])
async def test_native_discovery_pagination(kind, monkeypatch):
    original = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request)
        if kind == "google":
            assert request.headers["x-goog-api-key"] == "key"
            if len(requests) == 1:
                return httpx.Response(
                    200,
                    json={
                        "models": [
                            {
                                "name": "models/gemini",
                                "supportedGenerationMethods": [
                                    "generateContent"
                                ],
                                "inputTokenLimit": 1000000,
                            },
                            {
                                "name": "models/embedding",
                                "supportedGenerationMethods": ["embedContent"],
                            },
                            {
                                "name": "models/invalid/name",
                                "supportedGenerationMethods": [
                                    "generateContent"
                                ],
                            },
                            {
                                "name": "models/invalid",
                                "supportedGenerationMethods": 5,
                            },
                        ],
                        "nextPageToken": "next",
                    },
                )
            assert request.url.params["pageToken"] == "next"
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/next",
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ]
                },
            )
        assert request.headers["x-api-key"] == "key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "claude", "max_input_tokens": 200000}],
                    "has_more": True,
                    "last_id": "claude",
                },
            )
        assert request.url.params["after_id"] == "claude"
        return httpx.Response(
            200, json={"data": [{"id": "next"}], "has_more": False}
        )

    monkeypatch.setattr(
        discovery,
        "provider_client",
        lambda _base, **_kw: original(transport=httpx.MockTransport(respond)),
    )
    result = await _fetch_models(kind, "https://native.invalid/v1", "key")
    model, limit = (
        ("gemini", 1000000) if kind == "google" else ("claude", 200000)
    )
    assert result == {
        "models": [model, "next"],
        "model_contexts": {model: limit},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,mode,match",
    [
        ("google", "invalid_cursor", "Invalid model list cursor"),
        ("anthropic", "invalid_cursor", "Invalid model list cursor"),
        ("google", "repeat", "repeated a model list page"),
        ("google", "many_pages", "too many pages"),
        ("openai", "oversized", "model list is too large"),
    ],
)
async def test_discovery_bounds_and_pagination_errors(
    kind, mode, match, monkeypatch
):
    original = httpx.AsyncClient
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        assert "authorization" not in request.headers
        if mode == "oversized":
            return httpx.Response(200, content=b" " * (2 * 1024 * 1024 + 1))
        if kind == "anthropic":
            return httpx.Response(200, json={"data": [], "has_more": True})
        cursor = (
            5
            if mode == "invalid_cursor"
            else ("repeat" if mode == "repeat" else str(count))
        )
        return httpx.Response(200, json={"models": [], "nextPageToken": cursor})

    monkeypatch.setattr(
        discovery,
        "provider_client",
        lambda _base, **_kw: original(transport=httpx.MockTransport(respond)),
    )
    with pytest.raises(ValueError, match=match):
        await _fetch_models(kind, "http://127.0.0.1:8000/v1", "")
