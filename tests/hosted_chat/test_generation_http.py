"""Opt-in native generation transport with real Control auth and durable replay."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.support.hosted_chat_http import provider, runtime_for
from workgate.config.settings import Settings
from workgate.control.execution_context import control_execution_context
from workgate.control.http import hosted_chat
from workgate.control.http.app import build_http_app
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.storage import WorkspaceRepository
from workgate.oauth.core.urls import resource_url
from workgate.oauth.protocol.token_codec import issue_access_token

PREFIX = "/api/ui/chat/generations"


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


def test_generation_http_question_resolution_is_one_shot(tmp_path, monkeypatch):
    from workgate.hosted_chat.provider_common import ModelToolCall

    runtime = runtime_for(tmp_path)
    db = provider(runtime)
    original = hosted_chat.run_authenticated_generation
    calls = []

    async def stream(profile, messages, **kwargs):
        calls.append(messages)
        if not any(message["role"] == "tool" for message in messages):
            yield ModelToolCall(
                "question", "ask_user_question", {"question": "Which format?"}
            )
        else:
            assert "JSON" in str(messages[-1])
            yield "test answer"

    async def execute(*args, **kwargs):
        return await original(
            *args, **kwargs, stream_tools=stream, stream_text=stream
        )

    monkeypatch.setattr(hosted_chat, "run_authenticated_generation", execute)
    with TestClient(build_http_app(runtime=runtime)) as client:
        payload = {
            "text": "choose format",
            "provider": "test-provider",
            "model": "test-model",
            "ephemeralAgent": {"ask_user_question": True},
        }
        first = client.post(PREFIX, json=payload)
        assert first.status_code == 202
        run = first.json()["runId"]
        assert (
            "on_pending_action"
            in client.get(PREFIX + "/" + run + "/events").text
        )
        path = PREFIX + "/" + run + "/action"
        pending = client.get(path).json()["pendingAction"]
        assert pending["payload"]["type"] == "ask_user_question"
        body = {"actionId": pending["actionId"], "type": "ask_user_question"}
        assert (
            client.post(
                path, json={**body, "actionId": "wrong", "answer": "JSON"}
            ).status_code
            == 400
        )
        WorkspaceRepository(db).save_setting(
            "local-user",
            "webui.filters.config",
            {"messages": {"pii": {"starterPatterns": ["bearer_header"]}}},
        )
        rejected = client.post(path, json={**body, "answer": "Bearer private"})
        assert rejected.status_code == 400
        assert rejected.json()["error"] == "content_filter_block"
        assert client.get(path).json()["pendingAction"] == pending
        result = client.post(path, json={**body, "answer": "JSON"})
        assert result.status_code == 202, result.text
        assert result.json()["resolved"]
        assert "test answer" in client.get(PREFIX + "/" + run + "/events").text
        assert client.get(path).json()["pendingAction"] is None
        assert (
            client.post(path, json={**body, "answer": "JSON"}).status_code
            == 404
        )
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_generation_http_approval_scope_and_edited_policy(
    tmp_path, monkeypatch
):
    from tests.support.paired import build_paired_control_harness
    from workgate.hosted_chat.provider_common import ModelToolCall

    harness = build_paired_control_harness(
        Settings(
            default_workdir=tmp_path,
            state_dir=tmp_path / ".state",
            auth_mode="oauth",
            auth_bypass_localhost=False,
            hosted_chat_enabled=True,
            agent_bridge_enabled=False,
        )
    )
    runtime = harness.control
    db = provider(runtime)
    workspace = WorkspaceRepository(db)
    workspace.save_setting("alice", "approval.mode", "all")
    original = hosted_chat.run_authenticated_generation
    calls = []

    async def stream(profile, messages, **kwargs):
        calls.append(messages)
        if not any(message["role"] == "tool" for message in messages):
            yield ModelToolCall(
                "task-create", "task", {"action": "create", "label": "original"}
            )
        else:
            yield "test answer"

    async def execute(*args, **kwargs):
        return await original(
            *args, **kwargs, stream_tools=stream, stream_text=stream
        )

    monkeypatch.setattr(hosted_chat, "run_authenticated_generation", execute)
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

            alice = token("alice", "shell:read shell:write shell:execute")
            bob = token("bob", "shell:read shell:write shell:execute")
            narrow = token("alice", "shell:read")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_http_app(runtime=runtime)),
            base_url="http://testserver",
        ) as client:
            first = await client.post(
                PREFIX,
                json={
                    "text": "create task",
                    "taskMachine": harness.executor_id,
                    "provider": "test-provider",
                    "model": "test-model",
                },
                headers=alice,
            )
            assert first.status_code == 202, first.text
            run = first.json()["runId"]
            path = PREFIX + "/" + run + "/action"
            event = await client.get(
                PREFIX + "/" + run + "/events", headers=alice
            )
            assert "on_pending_action" in event.text, event.text
            pending = (await client.get(path, headers=alice)).json()[
                "pendingAction"
            ]
            assert pending["payload"]["type"] == "tool_approval"
            body = {
                "actionId": pending["actionId"],
                "type": "tool_approval",
                "resolutions": [
                    {
                        "tool_call_id": "task-create",
                        "decision": "edit",
                        "editedArguments": {
                            "action": "create",
                            "label": "private_123",
                        },
                    }
                ],
            }
            assert (await client.get(path, headers=bob)).status_code == 404
            assert (await client.get(path, headers=narrow)).status_code == 403
            assert (
                await client.post(path, json=body, headers=bob)
            ).status_code == 404
            assert (
                await client.post(path, json=body, headers=narrow)
            ).status_code == 403
            workspace.save_setting(
                "alice",
                "webui.filters.config",
                {
                    "toolArguments": {
                        "pii": {
                            "fields": ["arguments"],
                            "customPatterns": [
                                {
                                    "id": "private",
                                    "label": "private",
                                    "regex": "private_[0-9]+",
                                }
                            ],
                        }
                    }
                },
            )
            blocked = await client.post(path, json=body, headers=alice)
            assert (
                blocked.status_code == 400
                and blocked.json()["error"] == "content_filter_block"
            ), blocked.text
            assert "private_123" not in blocked.text
            assert (await client.get(path, headers=alice)).json()[
                "pendingAction"
            ] == pending
            body["resolutions"] = [
                {
                    "tool_call_id": "task-create",
                    "decision": "reject",
                    "reason": "do not create",
                }
            ]
            resolved = await client.post(path, json=body, headers=alice)
            assert resolved.status_code == 202, resolved.text
            response = await client.get(
                PREFIX + "/" + run + "/events", headers=alice
            )
            assert "test answer" in response.text, response.text
            assert (
                await client.post(path, json=body, headers=alice)
            ).status_code == 404
            assert len(calls) == 2


@pytest.mark.asyncio
async def test_generation_http_steering_exact_run_and_policy(
    tmp_path, monkeypatch
):
    runtime = runtime_for(tmp_path, auth_mode="oauth")
    db = provider(runtime)
    pause = asyncio.Event()
    fake_stream(monkeypatch, pause=pause)
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

            alice = token("alice", "shell:read shell:write")
            bob = token("bob", "shell:read shell:write")
            narrow = token("alice", "shell:read")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_http_app(runtime=runtime)),
            base_url="http://testserver",
        ) as client:
            first = await client.post(
                PREFIX,
                json={
                    "text": "question",
                    "provider": "test-provider",
                    "model": "test-model",
                },
                headers=alice,
            )
            assert first.status_code == 202, first.text
            run = first.json()["runId"]
            path = PREFIX + "/" + run + "/steering"
            steer = {"text": "new direction", "clientSteerId": "one"}
            assert (
                await client.post(path, json=steer, headers=bob)
            ).status_code == 404
            assert (
                await client.post(path, json=steer, headers=narrow)
            ).status_code == 403
            assert (
                await client.post(
                    path,
                    json={**steer, "conversationId": "wrong"},
                    headers=alice,
                )
            ).status_code == 409
            workspace = WorkspaceRepository(db)
            workspace.save_setting(
                "alice",
                "webui.filters.config",
                {"messages": {"pii": {"starterPatterns": ["bearer_header"]}}},
            )
            blocked = await client.post(
                path, json={**steer, "quotes": ["Bearer secret"]}, headers=alice
            )
            assert (
                blocked.status_code == 400
                and blocked.json()["error"] == "content_filter_block"
            )
            assert (await client.get(path, headers=alice)).json()[
                "pendingSteers"
            ] == []
            queued = await client.post(path, json=steer, headers=alice)
            assert queued.status_code == 200, queued.text
            sid = queued.json()["steerId"]
            assert (await client.post(path, json=steer, headers=alice)).json()[
                "replayed"
            ]
            assert (
                await client.post(
                    path + "/arm", json={"steerId": sid}, headers=alice
                )
            ).json()["armed"]
            assert (
                await client.post(
                    path + "/cancel",
                    json={"steerId": sid, "clientSteerId": "one"},
                    headers=alice,
                )
            ).json()["removed"]
            assert (await client.get(path, headers=alice)).json()[
                "pendingSteers"
            ] == []
            pause.set()
            assert (
                "test answer"
                in (
                    await client.get(
                        PREFIX + "/" + run + "/events", headers=alice
                    )
                ).text
            )
            successor = await client.post(
                PREFIX,
                json={
                    "text": "next",
                    "conversationId": first.json()["conversationId"],
                    "provider": "test-provider",
                    "model": "test-model",
                },
                headers=alice,
            )
            assert successor.status_code == 202, successor.text
            assert (
                await client.post(
                    path,
                    json={"text": "stale direction", "clientSteerId": "two"},
                    headers=alice,
                )
            ).status_code == 409
            assert (
                await client.post(
                    PREFIX + "/" + successor.json()["runId"] + "/steering",
                    json=steer,
                    headers=alice,
                )
            ).status_code == 409


@pytest.mark.asyncio
async def test_generation_http_abort_cancels_native_effect_and_driver(
    tmp_path, monkeypatch
):
    from tests.support.paired import build_paired_control_harness
    from workgate.hosted_chat.durable_tasks import DurableTaskRepository
    from workgate.hosted_chat.generation_actor_lease import ActorLease
    from workgate.hosted_chat.provider_common import ModelToolCall

    harness = build_paired_control_harness(
        Settings(
            default_workdir=tmp_path,
            state_dir=tmp_path / ".state",
            auth_mode="none",
            hosted_chat_enabled=True,
            agent_bridge_enabled=False,
        )
    )
    runtime = harness.control
    db = provider(runtime)
    entered = asyncio.Event()
    stopped = asyncio.Event()
    original = hosted_chat.run_authenticated_generation

    async def stream(profile, messages, **kwargs):
        if not any(message["role"] == "tool" for message in messages):
            yield ModelToolCall(
                "create", "task", {"action": "create", "label": "owned work"}
            )
        else:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            yield "unexpected"

    async def execute(*args, **kwargs):
        return await original(
            *args, **kwargs, stream_tools=stream, stream_text=stream
        )

    monkeypatch.setattr(hosted_chat, "run_authenticated_generation", execute)
    app = build_http_app(runtime=runtime)
    async with (
        runtime.lifespan(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        response = await client.post(
            PREFIX,
            json={
                "text": "work",
                "provider": "test-provider",
                "model": "test-model",
                "taskMachine": harness.executor_id,
            },
        )
        assert response.status_code == 202, response.text
        run = response.json()["runId"]
        await asyncio.wait_for(entered.wait(), timeout=5)
        [task] = await runtime.task_service.list_tasks()
        path = PREFIX + "/" + run
        queued = await client.post(
            path + "/steering",
            json={"text": "later", "clientSteerId": "queued"},
        )
        assert queued.status_code == 200
        aborted = await client.post(path + "/abort")
        assert (
            aborted.status_code == 200 and aborted.json()["status"] == "aborted"
        )
        assert DurableTaskRepository(db).is_run_cancelled(run)
        assert ActorLease.claim(db, "local-user", run) is None
        await asyncio.wait_for(stopped.wait(), timeout=5)
        assert await runtime.generation_supervisor.wait_idle("local-user", run)

        async def cleaned():
            while (
                await runtime.task_service.read_task(task.task_id)
            ).status != "cancelled":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(cleaned(), timeout=5)
        final = (await client.get(path + "/events")).text
        assert '"aborted":true' in final and '"pendingSteers"' in final
        assert (await client.post(path + "/abort")).status_code == 409
        assert (await client.post(path + "/attach")).status_code == 202
        assert not runtime.generation_supervisor.active("local-user", run)


def test_generation_http_abort_pending_question(tmp_path, monkeypatch):
    from workgate.hosted_chat.provider_common import ModelToolCall

    runtime = runtime_for(tmp_path)
    provider(runtime)
    original = hosted_chat.run_authenticated_generation

    async def stream(profile, messages, **kwargs):
        yield ModelToolCall(
            "question", "ask_user_question", {"question": "Choose?"}
        )

    async def execute(*args, **kwargs):
        return await original(
            *args, **kwargs, stream_tools=stream, stream_text=stream
        )

    monkeypatch.setattr(hosted_chat, "run_authenticated_generation", execute)
    with TestClient(build_http_app(runtime=runtime)) as client:
        response = client.post(
            PREFIX,
            json={
                "text": "choose",
                "provider": "test-provider",
                "model": "test-model",
                "ephemeralAgent": {"ask_user_question": True},
            },
        )
        assert response.status_code == 202
        path = PREFIX + "/" + response.json()["runId"]
        assert "on_pending_action" in client.get(path + "/events").text
        pending = client.get(path + "/action").json()["pendingAction"]
        assert client.post(path + "/abort").status_code == 200
        assert client.get(path + "/action").json()["pendingAction"] is None
        assert (
            client.post(
                path + "/action",
                json={
                    "actionId": pending["actionId"],
                    "type": "ask_user_question",
                    "answer": "yes",
                },
            ).status_code
            == 404
        )
        assert '"aborted":true' in client.get(path + "/events").text


def test_memory_reaches_provider_only(tmp_path, monkeypatch):
    from workgate.hosted_chat.memory import MemoryRepository

    runtime = runtime_for(tmp_path)
    db = provider(runtime)
    store = MemoryRepository(db)
    store.write("local-user", "preference", "private memory marker")
    original = hosted_chat.run_authenticated_generation
    calls = []

    async def stream(profile, messages, **kwargs):
        calls.append(json.dumps(messages))
        yield "ordinary response"

    async def execute(*args, **kwargs):
        return await original(
            *args, **kwargs, stream_tools=stream, stream_text=stream
        )

    monkeypatch.setattr(hosted_chat, "run_authenticated_generation", execute)
    payload = {
        "text": "question",
        "provider": "test-provider",
        "model": "test-model",
        "generationProtocolVersion": 2,
    }
    with TestClient(build_http_app(runtime=runtime)) as client:
        for enabled in (True, False):
            WorkspaceRepository(db).save_setting(
                "local-user", "user.memories", enabled
            )
            result = client.post(PREFIX, json=payload)
            assert result.status_code == 202, result.text
            run = result.json()["runId"]
            response = client.get(PREFIX + "/" + run + "/events")
            assert response.status_code == 200
            assert "private memory marker" not in response.text
            assert "ordinary response" in response.text
            assert ("private memory marker" in calls[-1]) is enabled
        # Only the memory table contains the stored value. It was not
        # copied into any conversation history or execution checkpoint.
        with db.read_snapshot() as connection:
            for table in ("messages", "runs"):
                rows = connection.execute(f"SELECT * FROM {table}").fetchall()
                assert "private memory marker" not in repr(
                    [dict(row) for row in rows]
                )


def test_memory_tool_requires_confirmation(tmp_path, monkeypatch):
    from workgate.hosted_chat.memory import MemoryRepository
    from workgate.hosted_chat.provider_common import ModelToolCall

    runtime = runtime_for(tmp_path)
    db = provider(runtime)
    original = hosted_chat.run_authenticated_generation

    async def stream(profile, messages, **kwargs):
        if not any(message["role"] == "tool" for message in messages):
            assert "set_memory" in [
                tool["function"]["name"] for tool in kwargs["tools"]
            ]
            yield ModelToolCall(
                "remember",
                "set_memory",
                {"key": "style", "value": "Use concise answers."},
            )
        else:
            yield "Saved."

    async def execute(*args, **kwargs):
        return await original(
            *args, **kwargs, stream_tools=stream, stream_text=stream
        )

    monkeypatch.setattr(hosted_chat, "run_authenticated_generation", execute)
    with TestClient(build_http_app(runtime=runtime)) as client:
        result = client.post(
            PREFIX,
            json={
                "text": "Remember concise answers",
                "provider": "test-provider",
                "model": "test-model",
            },
        )
        assert result.status_code == 202
        path = PREFIX + "/" + result.json()["runId"]
        assert "on_pending_action" in client.get(path + "/events").text
        assert MemoryRepository(db).list("local-user") == []
        action = client.get(path + "/action").json()["pendingAction"]
        response = client.post(
            path + "/action",
            json={
                "actionId": action["actionId"],
                "type": "tool_approval",
                "resolutions": [
                    {"tool_call_id": "remember", "decision": "approve"}
                ],
            },
        )
        assert response.status_code == 202, response.text
        assert "Saved." in client.get(path + "/events").text
        assert (
            MemoryRepository(db).list("local-user")[0]["value"]
            == "Use concise answers."
        )
