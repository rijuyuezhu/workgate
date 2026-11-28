"""Opt-in native generation transport with real Control auth and durable replay."""

import asyncio
import json
from typing import Literal

import httpx
import pytest
from fastapi.testclient import TestClient

from workgate.config.settings import Settings
from workgate.control.execution_context import control_execution_context
from workgate.control.http import hosted_chat
from workgate.control.http.app import build_http_app
from workgate.control.runtime import build_control_runtime
from workgate.hosted_chat.credential_cipher import CredentialCipher
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.provider_profiles import ProviderRepository
from workgate.hosted_chat.storage import WorkspaceRepository
from workgate.oauth.core.urls import resource_url
from workgate.oauth.protocol.token_codec import issue_access_token

PREFIX = "/api/ui/chat/generations"


def runtime_for(
    tmp_path,
    *,
    enabled=True,
    auth_mode: Literal["none", "oauth"] = "none",
    ui_enabled=True,
):
    return build_control_runtime(
        Settings(
            default_workdir=tmp_path,
            state_dir=tmp_path / ".state",
            auth_mode=auth_mode,
            auth_bypass_localhost=False,
            hosted_chat_enabled=enabled,
            ui_enabled=ui_enabled,
            agent_bridge_enabled=False,
        )
    )


def provider(runtime):
    db = SQLiteDatabase(runtime.config.state_dir)
    ProviderRepository(db, CredentialCipher(db)).save(
        {
            "name": "test-provider",
            "provider": "openai",
            "models": ["test-model"],
            "base_url": "https://models.invalid/v1",
            "api_key": "test-secret",
        }
    )
    return db


def fake_stream(monkeypatch, *, pause=None):
    original = hosted_chat.run_authenticated_generation
    calls = []

    async def stream(profile, messages, **kwargs):
        assert profile.token == "test-secret"
        calls.append(messages[-1]["content"])
        if pause is not None:
            await pause.wait()
        yield "test answer"

    async def execute(*args, **kwargs):
        return await original(
            *args, **kwargs, stream_tools=stream, stream_text=stream
        )

    monkeypatch.setattr(hosted_chat, "run_authenticated_generation", execute)
    return calls


@pytest.mark.parametrize("enabled,ui_enabled", [(False, True), (True, False)])
def test_generation_routes_off_by_default_and_no_db(
    tmp_path, enabled, ui_enabled
):
    runtime = runtime_for(tmp_path, enabled=enabled, ui_enabled=ui_enabled)
    with TestClient(build_http_app(runtime=runtime)) as client:
        assert client.post(PREFIX, json={"text": "no"}).status_code == 404
        assert not (runtime.config.state_dir / "hosted-chat").exists()


def test_generation_http_admission_replay_and_policy(tmp_path, monkeypatch):
    runtime = runtime_for(tmp_path)
    db = provider(runtime)
    calls = fake_stream(monkeypatch)
    payload = {
        "text": "question",
        "provider": "test-provider",
        "model": "test-model",
        "clientRequestId": "test-turn",
        "generationProtocolVersion": 2,
        "nativeAdmission": {"scopes": ["forged"], "task_machine": "forged"},
    }
    with TestClient(build_http_app(runtime=runtime)) as client:
        assert (
            client.post(
                PREFIX, json={**payload, "taskMachine": "unknown"}
            ).status_code
            == 400
        )
        result = client.post(PREFIX, json=payload)
        assert result.status_code == 202, result.text
        run = result.json()["runId"]
        assert not result.json()["resumed"]
        assert result.json()["generationProtocolVersion"] == 2
        response = client.get(
            PREFIX + "/" + run + "/events",
            headers={"X-LibreChat-Generation-Protocol": "2"},
        )
        assert response.status_code == 200
        assert response.headers["x-librechat-generation-protocol"] == "2"
        events = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert events[0]["created"] and events[-1]["final"]
        assert events[-1]["responseMessage"]["text"] == "test answer"
        assert "test-secret" not in response.text
        cursor = max(
            int(line[4:])
            for line in response.text.splitlines()
            if line.startswith("id: ")
        )
        assert (
            client.get(
                PREFIX + "/" + run + "/events",
                headers={"Last-Event-ID": str(cursor)},
            ).text
            == ""
        )
        assert (
            client.get(PREFIX + "/" + run + "/events?after=-1").status_code
            == 400
        )
        assert client.get(PREFIX + "/missing").status_code == 404
        assert client.get(PREFIX + "/" + run).json()["status"] == "complete"
        repeated = client.post(PREFIX, json=payload)
        assert repeated.json()["resumed"] and repeated.json()["runId"] == run
        assert client.post(PREFIX + "/" + run + "/attach").status_code == 202
        assert calls == ["question"]
        admission = GenerationStore(db).native_admission("local-user", run)
        assert admission is not None
        assert "forged" not in str(admission)
        WorkspaceRepository(db).save_setting(
            "local-user",
            "webui.filters.config",
            {"messages": {"pii": {"starterPatterns": ["bearer_header"]}}},
        )
        blocked = client.post(
            PREFIX,
            json={
                **payload,
                "clientRequestId": "blocked",
                "text": "Bearer private",
            },
        )
        assert (
            blocked.status_code == 400
            and blocked.json()["error"] == "content_filter_block"
        )
        assert "private" not in blocked.text


@pytest.mark.asyncio
async def test_generation_http_oauth_owner_and_scope_ceiling(
    tmp_path, monkeypatch
):
    runtime = runtime_for(tmp_path, auth_mode="oauth")
    db = provider(runtime)
    pause = asyncio.Event()
    calls = fake_stream(monkeypatch, pause=pause)
    app = build_http_app(runtime=runtime)
    async with runtime.lifespan():
        with control_execution_context(
            config=runtime.config,
            state_store=runtime.state_store,
            oauth_state=runtime.oauth_state,
        ):

            def token(owner, scopes):
                return {
                    "Authorization": "Bearer "
                    + issue_access_token(
                        client_id="chat-test",
                        scope=scopes,
                        resource=resource_url(),
                        subject=owner,
                    )
                }

            alice = token("alice", "shell:read")
            bob = token("bob", "shell:read")
            narrow = token("alice", "audit:read")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            body = {
                "text": "private question",
                "provider": "test-provider",
                "model": "test-model",
                "clientRequestId": "oauth-turn",
            }
            assert (await client.post(PREFIX, json=body)).status_code == 401
            first = await client.post(PREFIX, json=body, headers=alice)
            assert first.status_code == 202, first.text
            run = first.json()["runId"]
            assert (
                await client.get(PREFIX + "/" + run, headers=bob)
            ).status_code == 404
            assert (
                await client.get(PREFIX + "/" + run + "/events", headers=bob)
            ).status_code == 404
            assert (
                await client.post(PREFIX + "/" + run + "/attach", headers=bob)
            ).status_code == 404
            assert (
                await client.post(
                    PREFIX + "/" + run + "/attach", headers=narrow
                )
            ).status_code == 403
            assert (
                await client.post(PREFIX + "/" + run + "/attach", headers=alice)
            ).status_code == 202
            assert GenerationStore(db).native_admission("alice", run) == {
                "scopes": ["shell:read"],
                "task_machine": None,
            }
            pause.set()
            response = await client.get(
                PREFIX + "/" + run + "/events", headers=alice
            )
            assert (
                "test answer" in response.text
                and "test-secret" not in response.text
            )
            assert calls == ["private question"]


def test_generation_http_cookie_csrf_and_binding(tmp_path, monkeypatch):
    from workgate.oauth.protocol.token_codec import validate_bearer_token
    from workgate.ui.session import (
        UI_CSRF_HEADER,
        UI_SESSION_BINDING_HEADER,
        issue_ui_session,
        ui_csrf_cookie_name,
        ui_session_cookie_name,
    )

    runtime = runtime_for(tmp_path, auth_mode="oauth")
    provider(runtime)
    fake_stream(monkeypatch)
    origin = "http://testserver"
    binding = "b" * 43
    with control_execution_context(
        config=runtime.config,
        state_store=runtime.state_store,
        oauth_state=runtime.oauth_state,
    ):
        bearer = issue_access_token(
            client_id="browser-test",
            scope="shell:read",
            resource=resource_url(),
            subject="alice",
        )
        session, csrf, _ = issue_ui_session(
            validate_bearer_token(bearer), binding, origin
        )
    with TestClient(build_http_app(runtime=runtime), base_url=origin) as client:
        client.cookies.set(ui_session_cookie_name(origin), session)
        client.cookies.set(ui_csrf_cookie_name(origin), csrf)
        payload = {
            "text": "cookie question",
            "provider": "test-provider",
            "model": "test-model",
        }
        assert client.post(PREFIX, json=payload).status_code == 401
        headers = {UI_SESSION_BINDING_HEADER: binding, "Origin": origin}
        assert (
            client.post(PREFIX, json=payload, headers=headers).status_code
            == 403
        )
        headers[UI_CSRF_HEADER] = csrf
        result = client.post(PREFIX, json=payload, headers=headers)
        assert result.status_code == 202, result.text
        run = result.json()["runId"]
        assert (
            "test answer"
            in client.get(PREFIX + "/" + run + "/events", headers=headers).text
        )
