"""Controller channel CRUD and guarded discovery over the opt-in HTTP API."""

import httpx
import pytest

from tests.support.hosted_chat_http import provider, runtime_for
from workgate.control.execution_context import control_execution_context
from workgate.control.http.app import build_http_app
from workgate.hosted_chat import model_discovery
from workgate.hosted_chat.agents import AgentRepository
from workgate.hosted_chat.credential_cipher import CredentialCipher
from workgate.hosted_chat.provider_profiles import ProviderRepository
from workgate.oauth.core.urls import resource_url
from workgate.oauth.protocol.token_codec import issue_access_token

PREFIX = "/api/ui/chat/providers"


@pytest.mark.asyncio
async def test_provider_http_management_and_reference_guard(
    tmp_path, monkeypatch
):
    runtime = runtime_for(tmp_path, auth_mode="oauth")
    db = provider(runtime)
    repo = ProviderRepository(db, CredentialCipher(db))
    app = build_http_app(runtime=runtime)
    calls = []

    async def fetch(kind, base, token, **kwargs):
        calls.append((kind, base, token))
        return {
            "models": ["discovered"],
            "model_contexts": {"discovered": 8192},
        }

    monkeypatch.setattr(model_discovery, "_fetch_models", fetch)
    async with (
        runtime.lifespan(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        with control_execution_context(
            config=runtime.config,
            state_store=runtime.state_store,
            oauth_state=runtime.oauth_state,
        ):

            def headers(owner, scope):
                return {
                    "Authorization": "Bearer "
                    + issue_access_token(
                        client_id="provider-test",
                        scope=scope,
                        resource=resource_url(),
                        subject=owner,
                    )
                }

            admin = headers("local-user", "shell:read shell:write")
            narrow = headers("local-user", "shell:read")
            other = headers("other", "shell:read shell:write")
        assert (await client.get(PREFIX)).status_code == 401
        listing = await client.get(PREFIX, headers=narrow)
        assert listing.status_code == 200
        assert (
            "test-secret" not in listing.text and "api_key" not in listing.text
        )
        payload = {
            "name": "discovery-channel",
            "provider": "openai",
            "base_url": "https://models.invalid/v1",
            "api_key": "private-key",
            "models": ["manual"],
            "auto_discover": True,
        }
        for denied in (narrow, other):
            assert (
                await client.put(PREFIX, json=payload, headers=denied)
            ).status_code == 403
            assert (
                await client.post(
                    PREFIX + "/discover", json=payload, headers=denied
                )
            ).status_code == 403
        assert not calls
        saved = await client.put(PREFIX, json=payload, headers=admin)
        assert saved.status_code == 200, saved.text
        assert "private-key" not in saved.text
        assert repo.profile("discovery-channel")["api_key"] == "private-key"
        assert repo.profile("discovery-channel")["models"] == ["discovered"]
        assert (
            await client.post(
                PREFIX + "/refresh",
                json={"channel": "discovery-channel"},
                headers=admin,
            )
        ).status_code == 200
        assert (
            await client.post(
                PREFIX + "/discover",
                json={
                    **payload,
                    "api_key": None,
                    "existing_channel": "discovery-channel",
                },
                headers=admin,
            )
        ).json()["models"] == ["discovered"]
        assert len(calls) == 3
        AgentRepository(db).create(
            "other", {"provider": "discovery-channel", "model": "discovered"}
        )
        deletion = await client.request(
            "DELETE", PREFIX, json={"name": "discovery-channel"}, headers=admin
        )
        assert deletion.status_code == 409 and deletion.json()["can_force"]
        assert (
            await client.request(
                "DELETE",
                PREFIX,
                json={"name": "discovery-channel", "force": True},
                headers=admin,
            )
        ).status_code == 200
        with pytest.raises(ValueError, match="Unknown model channel"):
            repo.profile("discovery-channel")


@pytest.mark.asyncio
async def test_discovery_rechecks_grant_before_committing(
    tmp_path, monkeypatch
):
    runtime = runtime_for(tmp_path)
    db = provider(runtime)
    repo = ProviderRepository(db, CredentialCipher(db))
    authorized = True

    def check():
        if not authorized:
            raise PermissionError("grant expired")

    async def fetch(*args, **kwargs):
        nonlocal authorized
        authorized = False
        return {"models": ["discovered"], "model_contexts": {}}

    monkeypatch.setattr(model_discovery, "_fetch_models", fetch)
    payload = {
        "name": "new-channel",
        "provider": "openai",
        "base_url": "https://models.invalid/v1",
        "api_key": "private-key",
        "auto_discover": True,
        "models": ["manual"],
    }
    with pytest.raises(PermissionError):
        await model_discovery.save_discovered(repo, payload, check=check)
    with pytest.raises(ValueError, match="Unknown model channel"):
        repo.profile("new-channel")
    await model_discovery.save_discovered(repo, payload)
    before = repo.public_profiles()
    authorized = True
    with pytest.raises(PermissionError):
        await model_discovery.refresh_model_channel(
            repo, "new-channel", check=check
        )
    assert repo.public_profiles() == before
