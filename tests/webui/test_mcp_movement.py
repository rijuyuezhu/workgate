"""Moving MCP ownership from the existing editor in one Apply transaction."""

from fastapi.testclient import TestClient

from tests.support.paired import build_paired_http_app
from workgate.config.settings import Settings

BASE = "https://workgate.example"


def test_mcp_location_apply_control_executor_roundtrip(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    app, harness = build_paired_http_app(
        Settings(
            default_workdir=project,
            state_dir=tmp_path / "state",
            auth_mode="none",
            base_url=BASE,
            agent_bridge_enabled=True,
        )
    )

    def get(client, owner, name):
        r = client.get(
            "/api/ui/integrations",
            params={
                "owner": owner,
                "executor_id": harness.executor_id,
                "kind": "mcp",
                "action": "get",
                "name": name,
            },
        )
        assert r.status_code == 200, r.text
        return r.json()["data"]["server"]

    def rows(client, owner):
        r = client.get(
            "/api/ui/integrations",
            params={
                "owner": owner,
                "executor_id": harness.executor_id,
                "kind": "mcp",
            },
        )
        assert r.status_code == 200, r.text
        return r.json()["data"]["servers"]

    def create(client, owner, name, config):
        r = client.post(
            "/api/ui/integrations",
            json={
                "owner": owner,
                "executor_id": harness.executor_id,
                "kind": "mcp",
                "action": "register",
                "name": name,
                "config": config,
            },
        )
        assert r.status_code == 200, r.text

    def move(client, owner, dst, name, config, revision=None):
        return client.post(
            "/api/ui/integrations/move",
            json={
                "owner": owner,
                "executor_id": harness.executor_id,
                "destination_owner": dst,
                "destination_executor_id": harness.executor_id,
                "name": name,
                "expected_revision": revision
                or get(client, owner, name)["revision"],
                "config": config,
            },
        )

    with TestClient(app, base_url=BASE) as client:
        create(
            client,
            "control",
            "moveme",
            {
                "type": "http",
                "url": "https://example.org/mcp",
                "enabled": False,
            },
        )
        changed = move(
            client,
            "control",
            "executor",
            "moveme",
            {
                "type": "stdio",
                "command": "echo",
                "args": ["hello"],
                "enabled": False,
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["data"]["moved"]
        assert not rows(client, "control")
        assert [
            (row["name"], row["enabled"]) for row in rows(client, "executor")
        ] == [("moveme", False)]

        changed = move(
            client,
            "executor",
            "control",
            "moveme",
            {
                "type": "http",
                "url": "https://example.org/new",
                "enabled": True,
            },
        )
        assert changed.status_code == 200, changed.text
        assert not rows(client, "executor")
        assert [
            (row["name"], row["enabled"]) for row in rows(client, "control")
        ] == [("moveme", True)]

        stale = move(
            client,
            "control",
            "executor",
            "moveme",
            {
                "type": "stdio",
                "command": "echo",
            },
            revision="a" * 64,
        )
        assert stale.status_code == 409
        assert len(rows(client, "control")) == 1
        assert not rows(client, "executor")

        # Target name collision leaves both configurations intact.
        create(
            client, "executor", "moveme", {"type": "stdio", "command": "printf"}
        )
        collision = move(
            client,
            "control",
            "executor",
            "moveme",
            {
                "type": "stdio",
                "command": "echo",
            },
        )
        assert collision.status_code == 409
        assert len(rows(client, "control")) == 1
        assert [
            (x["name"], x["enabled"]) for x in rows(client, "executor")
        ] == [("moveme", True)]

        # An authenticated target does not silently acquire source-host secret refs.
        denied = move(
            client,
            "control",
            "executor",
            "moveme",
            {
                "type": "stdio",
                "command": "echo",
                "auth": {"mode": "secret"},
                "env": {"KEY": {"secret": "api_key"}},
            },
        )
        assert denied.status_code == 409
        assert len(rows(client, "control")) == 1

        # Connection type must agree with the owning runtime.
        invalid = move(
            client,
            "control",
            "executor",
            "moveme",
            {
                "type": "http",
                "url": "https://example.org/mcp",
            },
        )
        assert invalid.status_code == 400
        assert len(rows(client, "control")) == 1


def test_move_between_two_executor_hosts(tmp_path):
    """Both sides execute their own manifest RPC, not a Control-side copy."""
    from dataclasses import replace
    from typing import Any, cast

    from pydantic import JsonValue, TypeAdapter

    from workgate.config.executor import resolve_executor_config
    from workgate.control.state import ExecutorTrustRecord
    from workgate.executor.connection import operation_error_from_exception
    from workgate.executor.runtime import build_executor_runtime
    from workgate.persistence import use_state_store
    from workgate.protocol.credentials import (
        executor_credential_verifier,
        new_executor_credential,
    )
    from workgate.protocol.executor import ExecutorCommand, ExecutorResult
    from workgate.protocol.ids import new_command_id, new_executor_id
    from workgate.utils.serialization import to_jsonable

    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(
        default_workdir=project,
        state_dir=tmp_path / "control",
        auth_mode="none",
        base_url=BASE,
    )
    app, harness = build_paired_http_app(settings)
    second_settings = settings.model_copy(
        update={"state_dir": tmp_path / "second-executor"}
    )
    second = build_executor_runtime(
        replace(
            resolve_executor_config(second_settings),
            agent_config_dir=tmp_path / "second-agent",
        ),
        enable_control_connection=False,
    )
    second_id = str(new_executor_id())
    harness.control.control_state.put_executor(
        ExecutorTrustRecord(
            executor_id=second_id,
            name="second-executor",
            credential_verifier=executor_credential_verifier(
                new_executor_credential()
            ),
            created_at=2.0,
        )
    )
    original_call = harness.call

    async def online(executor_id):
        return executor_id in {harness.executor_id, second_id}

    async def call(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        if executor_id != second_id:
            return await original_call(
                executor_id,
                op,
                args,
                session_id=session_id,
                timeout_s=timeout_s,
            )
        command = ExecutorCommand(
            id=new_command_id(), op=op, session_id=session_id, args=args or {}
        )
        with use_state_store(second.services.state_store):
            try:
                result = await second._execute_protocol_command(command)
            except Exception as exc:
                return ExecutorResult(
                    id=command.id,
                    ok=False,
                    error=operation_error_from_exception(exc),
                )
            return ExecutorResult(
                id=command.id,
                ok=True,
                result=TypeAdapter(JsonValue).validate_python(
                    to_jsonable(result)
                ),
            )

    transport = cast(Any, harness.control.executor_transport)
    transport.is_online = online
    transport.call = call
    with TestClient(app, base_url=BASE) as client:
        created = client.post(
            "/api/ui/integrations",
            json={
                "owner": "executor",
                "executor_id": harness.executor_id,
                "kind": "mcp",
                "action": "register",
                "name": "local-transfer",
                "config": {
                    "type": "stdio",
                    "command": "echo",
                    "args": ["first"],
                },
            },
        )
        assert created.status_code == 200, created.text
        original = client.get(
            "/api/ui/integrations",
            params={
                "owner": "executor",
                "executor_id": harness.executor_id,
                "kind": "mcp",
                "action": "get",
                "name": "local-transfer",
            },
        ).json()["data"]["server"]
        moved = client.post(
            "/api/ui/integrations/move",
            json={
                "owner": "executor",
                "executor_id": harness.executor_id,
                "destination_owner": "executor",
                "destination_executor_id": second_id,
                "name": "local-transfer",
                "expected_revision": original["revision"],
                "config": {
                    "type": "stdio",
                    "command": "printf",
                    "args": ["second"],
                },
            },
        )
        assert moved.status_code == 200, moved.text
        src = client.get(
            "/api/ui/integrations",
            params={
                "owner": "executor",
                "executor_id": harness.executor_id,
                "kind": "mcp",
            },
        ).json()["data"]["servers"]
        dst = client.get(
            "/api/ui/integrations",
            params={
                "owner": "executor",
                "executor_id": second_id,
                "kind": "mcp",
            },
        ).json()["data"]["servers"]
        assert src == []
        assert [(row["name"], row["enabled"]) for row in dst] == [
            ("local-transfer", True)
        ]


def test_source_remove_conflict_rolls_back_target(tmp_path, monkeypatch):
    """An actual source CAS failure must not leave a duplicate MCP active."""
    from workgate.control.http import executor_admin

    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(
        default_workdir=project,
        state_dir=tmp_path / "state",
        auth_mode="none",
        base_url=BASE,
    )
    app, harness = build_paired_http_app(settings)
    with TestClient(app, base_url=BASE) as client:
        registered = client.post(
            "/api/ui/integrations",
            json={
                "owner": "control",
                "kind": "mcp",
                "action": "register",
                "name": "cas-failure",
                "config": {"type": "http", "url": "https://example.org/mcp"},
            },
        )
        assert registered.status_code == 200, registered.text
        control = client.get(
            "/api/ui/integrations",
            params={
                "owner": "control",
                "kind": "mcp",
                "action": "get",
                "name": "cas-failure",
            },
        ).json()["data"]["server"]
        original = executor_admin.manage_mcp_manifest

        def race(config_dir, action, *, name=None, **kwargs):
            if action == "remove" and name == "cas-failure":
                raise ValueError("source revision changed")
            return original(config_dir, action, name=name, **kwargs)

        monkeypatch.setattr(executor_admin, "manage_mcp_manifest", race)
        reply = client.post(
            "/api/ui/integrations/move",
            json={
                "owner": "control",
                "destination_owner": "executor",
                "destination_executor_id": harness.executor_id,
                "name": "cas-failure",
                "expected_revision": control["revision"],
                "config": {"type": "stdio", "command": "echo"},
            },
        )
        assert reply.status_code == 409, reply.text
        assert reply.json()["message"].endswith("rolled back")
        assert (
            client.get(
                "/api/ui/integrations",
                params={
                    "owner": "executor",
                    "executor_id": harness.executor_id,
                    "kind": "mcp",
                },
            ).json()["data"]["servers"]
            == []
        )
        assert (
            len(
                client.get(
                    "/api/ui/integrations",
                    params={
                        "owner": "control",
                        "kind": "mcp",
                    },
                ).json()["data"]["servers"]
            )
            == 1
        )
