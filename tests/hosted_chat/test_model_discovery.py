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


@pytest.fixture
def profiles(tmp_path):
    from workgate.hosted_chat.credential_cipher import CredentialCipher
    from workgate.hosted_chat.database import SQLiteDatabase
    from workgate.hosted_chat.provider_profiles import ProviderRepository

    db = SQLiteDatabase(tmp_path)
    return ProviderRepository(db, CredentialCipher(db))


def profile_body(**changes):
    return {
        "name": "Local",
        "provider": "openai",
        "base_url": "https://models.invalid/v1",
        "models": ["old"],
        **changes,
    }


@pytest.mark.asyncio
async def test_discovery_filtering_and_saved_key_binding(profiles, monkeypatch):
    profiles.save(profile_body(api_key="stored-secret"))
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "chat", "context_length": 10000},
                    {"id": "embeddings-v2"},
                    {"id": "custom-embed", "task": "embedding"},
                    {
                        "id": "vision",
                        "architecture": {"output_modalities": ["image"]},
                    },
                    {
                        "id": "responses-only",
                        "supported_endpoints": ["/responses"],
                    },
                    {
                        "id": "compatible",
                        "supported_endpoints": ["/v1/chat/completions"],
                    },
                ]
            },
        )

    monkeypatch.setattr(
        discovery,
        "provider_client",
        lambda _base, **_kw: httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ),
    )
    result = await discovery.discover_models(
        profiles, profile_body(existing_channel="Local")
    )
    assert result == {
        "models": ["chat", "compatible"],
        "model_contexts": {"chat": 10000},
    }
    assert requests[-1].headers["Authorization"] == "Bearer stored-secret"
    with pytest.raises(ValueError, match="changed endpoint"):
        await discovery.discover_models(
            profiles,
            profile_body(
                existing_channel="Local", base_url="https://other.invalid/v1"
            ),
        )
    assert len(requests) == 1
    await discovery.discover_models(
        profiles,
        profile_body(
            existing_channel="Local",
            base_url="https://other.invalid/v1",
            keyless=True,
        ),
    )
    assert "Authorization" not in requests[-1].headers
    await discovery.discover_models(
        profiles,
        profile_body(
            existing_channel="Local",
            base_url="https://other.invalid/v1",
            api_key="new-secret",
        ),
    )
    assert requests[-1].headers["Authorization"] == "Bearer new-secret"


@pytest.mark.asyncio
async def test_discovery_does_not_expose_provider_error(profiles, monkeypatch):
    def respond(request):
        return httpx.Response(401, json={"error": "private-secret"})

    monkeypatch.setattr(
        discovery,
        "provider_client",
        lambda _base, **_kw: httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ),
    )
    with pytest.raises(discovery.CatalogUnavailable) as failure:
        await discovery.discover_models(
            profiles, profile_body(api_key="private-secret")
        )
    assert "private-secret" not in str(failure.value)
    assert not any(
        row["configured"] for row in profiles.public_profiles()["providers"]
    )


def auto_profile(profiles):
    import json

    profiles.save(
        profile_body(
            api_key="stored-secret",
            model_contexts={"old": 4096},
            model_sampling={"old": {"temperature": 0}},
        )
    )
    with profiles.database.connect() as db:
        value = json.loads(
            db.execute(
                "SELECT value FROM settings WHERE key='model.channel.Local'"
            ).fetchone()[0]
        )
        value.update(
            auto_discover=True,
            discovered_model_contexts={"old": 8192},
            model_context_overrides={"old": 4096},
            model_sync_at=1,
        )
        raw = json.dumps(value)
        db.execute(
            "UPDATE settings SET value=? WHERE key='model.channel.Local'",
            (raw,),
        )
    return raw


@pytest.mark.asyncio
async def test_refresh_keeps_last_success_and_context_overrides(
    profiles, monkeypatch
):
    raw = auto_profile(profiles)

    async def failure(*args, **kwargs):
        raise httpx.ConnectError("private upstream error")

    monkeypatch.setattr(discovery, "_fetch_models", failure)
    assert await discovery._refresh_snapshot(
        profiles, "model.channel.Local", raw
    )
    value = profiles.profile("Local")
    assert value["models"] == ["old"] and value["model_sync_at"] == 1
    assert value["model_contexts"] == {"old": 4096}
    assert value["model_sync_error"] == discovery._SYNC_ERROR

    async def success(kind, base, token, **kwargs):
        assert (kind, base, token) == (
            "openai",
            "https://models.invalid/v1",
            "stored-secret",
        )
        return {
            "models": ["old", "new"],
            "model_contexts": {"old": 10000, "new": 20000},
        }

    monkeypatch.setattr(discovery, "_fetch_models", success)
    await discovery.refresh_model_channel(profiles, "Local")
    current = profiles.profile("Local")
    assert current["models"] == ["old", "new"]
    assert current["model_contexts"] == {"old": 4096, "new": 20000}
    assert current["discovered_model_contexts"] == {"old": 10000, "new": 20000}
    assert current["model_sampling"] == {"old": {"temperature": 0}}
    assert current["model_sync_error"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["edit", "delete"])
async def test_refresh_cas_rejects_stale_reply(profiles, monkeypatch, change):
    raw = auto_profile(profiles)

    async def fetch(*args, **kwargs):
        if change == "edit":
            profiles.save(profile_body(models=["manual"], api_key="new-secret"))
        else:
            profiles.delete(name="Local")
        return {"models": ["stale"], "model_contexts": {}}

    monkeypatch.setattr(discovery, "_fetch_models", fetch)
    assert not await discovery._refresh_snapshot(
        profiles, "model.channel.Local", raw
    )
    if change == "edit":
        assert profiles.profile("Local")["models"] == ["manual"]
        assert profiles.profile("Local")["api_key"] == "new-secret"
    else:
        with pytest.raises(ValueError, match="Unknown model channel"):
            profiles.profile("Local")


@pytest.mark.asyncio
async def test_auto_save_fallback_requires_same_connection(
    profiles, monkeypatch
):
    import json

    raw = auto_profile(profiles)
    previous = json.loads(raw)

    async def failed(*args, **kwargs):
        raise httpx.ConnectError("private detail")

    monkeypatch.setattr(discovery, "_fetch_models", failed)
    body = profile_body(auto_discover=True, model_contexts={"old": 4096})
    from workgate.hosted_chat.provider_profiles import _validate

    value = _validate(body)
    assert await discovery.prepare_auto_save(profiles, value, previous) is None
    discovery.apply_auto_save(value, previous, None)
    assert (
        value["model_sync_at"] == 1
        and value["model_sync_error"] == discovery._SYNC_ERROR
    )
    assert value["model_contexts"] == {"old": 4096}
    with pytest.raises(ValueError, match="changed endpoint"):
        await discovery.prepare_auto_save(
            profiles,
            _validate({**body, "base_url": "https://other.invalid/v1"}),
            previous,
        )
    with pytest.raises(discovery.CatalogUnavailable):
        await discovery.prepare_auto_save(
            profiles, _validate({**body, "api_key": "different"}), previous
        )


@pytest.mark.asyncio
async def test_auto_save_catalog_and_stale_write(profiles, monkeypatch):
    from workgate.hosted_chat.provider_profiles import ChannelConflict

    async def success(kind, base, token, **kwargs):
        assert token == "stored-secret"
        return {
            "models": ["old", "new"],
            "model_contexts": {"old": 8192, "new": 16384},
        }

    monkeypatch.setattr(discovery, "_fetch_models", success)
    await profiles.save_discovered(
        profile_body(
            auto_discover=True,
            api_key="stored-secret",
            model_contexts={"old": 4096},
            model_sampling={"old": {"temperature": 0}},
        )
    )
    current = profiles.profile("Local")
    assert current["models"] == ["old", "new"]
    assert current["model_contexts"] == {"old": 4096, "new": 16384}
    assert (
        current["auto_discover"] is True
        and current["api_key"] == "stored-secret"
    )

    async def unavailable(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(discovery, "_fetch_models", unavailable)
    await profiles.save_discovered(
        profile_body(
            auto_discover=True,
            models=["old", "new"],
            model_contexts={"old": 4096},
        )
    )
    saved = profiles.profile("Local")
    assert saved["model_sync_at"] == current["model_sync_at"]
    assert saved["models"] == current["models"]
    assert saved["model_sampling"] == {"old": {"temperature": 0}}
    assert saved["model_sync_error"] == discovery._SYNC_ERROR

    async def stale(*args, **kwargs):
        profiles.save(profile_body(api_key="changed", models=["manual"]))
        return {"models": ["stale"], "model_contexts": {}}

    monkeypatch.setattr(discovery, "_fetch_models", stale)
    with pytest.raises(ChannelConflict, match="changed during discovery"):
        await profiles.save_discovered(profile_body(auto_discover=True))
    assert profiles.profile("Local")["models"] == ["manual"]
    assert profiles.profile("Local")["api_key"] == "changed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,account,match",
    [
        ([], False, "Invalid provider model list"),
        ({"data": {}}, False, "Invalid provider model list"),
        ({"data": []}, False, "no supported models"),
        ({"data": [{"id": str(i)} for i in range(129)]}, False, "128 models"),
        ({}, True, "Invalid account model list"),
    ],
)
async def test_invalid_catalog_response(monkeypatch, payload, account, match):
    monkeypatch.setattr(
        discovery,
        "provider_client",
        lambda *_args, **_kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload)
            )
        ),
    )
    with pytest.raises(ValueError, match=match):
        await _fetch_models(
            "openai", "https://models.invalid", "", account=account
        )


@pytest.mark.asyncio
async def test_catalog_refresh_only_opted_in_channels(
    profiles, monkeypatch, caplog
):
    import asyncio

    auto_profile(profiles)
    profiles.save(profile_body(name="manual", models=["manual"]))
    calls = []

    async def fetch(kind, base, token, **kwargs):
        calls.append((kind, base, token))
        return {"models": [], "model_contexts": {}}

    monkeypatch.setattr(discovery, "_fetch_models", fetch)
    await discovery.refresh_model_channels(profiles)
    assert calls == [("openai", "https://models.invalid/v1", "stored-secret")]
    assert profiles.profile("Local")["models"] == []
    assert profiles.profile("manual")["models"] == ["manual"]
    with pytest.raises(ValueError, match="disabled"):
        await discovery.refresh_model_channel(profiles, "manual")
    with pytest.raises(KeyError, match="not found"):
        await discovery.refresh_model_channel(profiles, "missing")
    with pytest.raises(ValueError, match="saved channel"):
        await discovery.refresh_model_channel(profiles, None)  # type: ignore[arg-type]

    async def refresh_failed(*args):
        raise RuntimeError("private transport data")

    monkeypatch.setattr(discovery, "_refresh_snapshot", refresh_failed)
    await discovery.refresh_model_channels(profiles)
    assert "RuntimeError" in caplog.text
    assert "private transport data" not in caplog.text

    monkeypatch.setattr(discovery, "refresh_model_channels", refresh_failed)

    async def stop(interval):
        assert interval == discovery.REFRESH_INTERVAL
        raise asyncio.CancelledError

    monkeypatch.setattr(discovery.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await discovery.serve_model_refresh(profiles)
    assert "refresh cycle failed" in caplog.text
    assert "private transport data" not in caplog.text


@pytest.mark.asyncio
async def test_auto_save_manual_fallback_and_keyless_discovery(
    profiles, monkeypatch
):
    profiles.save(
        profile_body(model_contexts={"old": 4096}, api_key="stored-secret")
    )

    async def unavailable(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(discovery, "_fetch_models", unavailable)
    await profiles.save_discovered(profile_body(auto_discover=True))
    saved = profiles.profile("Local")
    assert saved["models"] == ["old"]
    assert saved["model_contexts"] == {"old": 4096}
    assert saved["discovered_model_contexts"] == {}
    assert saved["model_sync_error"] == discovery._SYNC_ERROR

    async def fetch(kind, base, token, **kwargs):
        assert token == ""
        return {"models": ["new"], "model_contexts": {}}

    monkeypatch.setattr(discovery, "_fetch_models", fetch)
    await profiles.save_discovered(
        profile_body(auto_discover=True, keyless=True)
    )
    assert not profiles.profile("Local").get("api_key")
    assert profiles.profile("Local")["models"] == ["new"]
