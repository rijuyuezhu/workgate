"""HTTP authorization preserves the exact approved OAuth interruption."""

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.hosted_chat.test_action_oauth_flow import BASE, pause, started
from workgate.config.settings import Settings
from workgate.control.execution_context import control_execution_context
from workgate.control.http.app import build_http_app
from workgate.control.runtime import build_control_runtime
from workgate.hosted_chat import action_oauth_flow as oauth
from workgate.hosted_chat.approval_claims import ApprovalClaimStore
from workgate.oauth.core.urls import resource_url
from workgate.oauth.protocol.token_codec import issue_access_token

oauth_started = started

START = "/api/ui/chat/actions/oauth/start"
CALLBACK = "/api/ui/chat/actions/oauth/callback"


def runtime_for(state_dir, *, enabled=True, base=BASE):
    return build_control_runtime(
        Settings(
            default_workdir=state_dir,
            state_dir=state_dir,
            base_url=base,
            auth_mode="oauth",
            auth_bypass_localhost=False,
            hosted_chat_enabled=enabled,
            agent_bridge_enabled=False,
        )
    )


@pytest.mark.asyncio
async def test_authorized_start_and_public_callback(oauth_started, monkeypatch):
    db, _, _, saved, job, _, _, _ = oauth_started
    flows, pending = pause(oauth_started)
    runtime = runtime_for(db.root.parent)
    seen = []

    async def exchange(endpoint, form, credentials, method):
        seen.append(form)
        return {"access_token": "test-access"}

    monkeypatch.setattr(oauth, "exchange", exchange)
    async with (
        runtime.lifespan(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_http_app(runtime=runtime)),
            base_url=BASE,
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
                        client_id="action-test",
                        scope=scope,
                        resource=resource_url(),
                        subject=owner,
                    )
                }

            alice = headers("alice", "shell:read shell:write")
            bob = headers("bob", "shell:read shell:write")
            read = headers("alice", "shell:read")
        selection = {"runId": job.run_id, "actionId": pending["actionId"]}
        assert (await client.post(START, json=selection)).status_code == 401
        assert (
            await client.post(START, json=selection, headers=read)
        ).status_code == 403
        assert (
            await client.post(START, json=selection, headers=bob)
        ).status_code == 404
        response = await client.post(START, json=selection, headers=alice)
        assert response.status_code == 200, response.text
        cookie = response.headers["set-cookie"]
        assert all(
            value in cookie.lower()
            for value in [
                "httponly",
                "secure",
                "samesite=lax",
                "path=" + CALLBACK,
            ]
        )
        assert response.headers["cache-control"] == "no-store"
        data = response.json()
        assert set(data) == {"authorizationUrl", "expiresAt"}
        state = parse_qs(urlsplit(data["authorizationUrl"]).query)["state"][0]
        params = {"state": state, "code": "code"}
        async with httpx.AsyncClient(
            transport=client._transport, base_url=BASE
        ) as foreign:
            assert (
                await foreign.get(CALLBACK, params=params)
            ).status_code == 400
        assert (
            await client.get(
                CALLBACK, params=params, headers={"host": "wrong.example.org"}
            )
        ).status_code == 400
        assert not seen
        callback = await client.get(CALLBACK, params=params)
        assert callback.status_code == 200, callback.text
        assert callback.headers["cache-control"] == "no-store"
        assert callback.headers["referrer-policy"] == "no-referrer"
        assert "test-access" not in callback.text
        assert (
            await flows.tokens.access_token("alice", saved["action_id"], 1)
            == "test-access"
        )
        assert ApprovalClaimStore(db).pending("alice", job.run_id) is None
        assert (await client.get(CALLBACK, params=params)).status_code == 400
        assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled,base,expected", [(False, BASE, 401), (True, None, 400)]
)
async def test_callback_disabled_or_unconfigured(
    tmp_path, enabled, base, expected
):
    runtime = runtime_for(tmp_path, enabled=enabled, base=base)
    async with (
        runtime.lifespan(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_http_app(runtime=runtime)),
            base_url=BASE,
        ) as client,
    ):
        assert (await client.get(CALLBACK)).status_code == expected
