"""The Executor must report successful Apply to Control before restarting."""

from typing import Any, cast

import pytest
from pydantic import JsonValue

from workgate import supervisor
from workgate.config import editing, inspection
from workgate.config.editing import ConfigEditError
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor.connection import ExecutorConnection
from workgate.executor.runtime import build_executor_runtime
from workgate.protocol.executor import ExecutorCommand
from workgate.protocol.ids import new_command_id


@pytest.mark.asyncio
async def test_config_apply_validates_file_and_parent_before_ack(
    tmp_path, monkeypatch
):
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                default_workdir=tmp_path / "workspace",
                state_dir=tmp_path / "state",
            )
        )
    )
    snapshot = object()
    monkeypatch.setattr(
        inspection, "current_startup_config", lambda role: snapshot
    )
    monkeypatch.setattr(supervisor, "restart_status", lambda: "healthy")
    calls = []

    def validate(selected, *, revision):
        calls.append((selected, revision))

    monkeypatch.setattr(editing, "validate_apply", validate)
    args: dict[str, JsonValue] = {"revision": "a" * 64}
    command = ExecutorCommand(
        id=new_command_id(), op="ui.config.apply", args=args
    )
    assert await runtime._execute_protocol_command_with_state(command) == {
        "status": "queued"
    }
    assert calls == [(snapshot, "a" * 64)]

    def reject(*a, **kw):
        raise ConfigEditError("RevisionConflict", 409)

    monkeypatch.setattr(editing, "validate_apply", reject)
    assert await runtime._execute_protocol_command_with_state(command) == {
        "error": "RevisionConflict",
        "status": 409,
    }
    monkeypatch.setattr(supervisor, "restart_status", lambda: "unmanaged")
    assert await runtime._execute_protocol_command_with_state(command) == {
        "error": "ParentUnavailable",
        "status": 409,
    }


@pytest.mark.asyncio
async def test_remote_parent_restart_follows_successful_control_delivery(
    monkeypatch,
):
    events = []

    class Client:
        async def submit_result(self, result):
            events.append(("submit", result.ok))

    client = Client()
    monkeypatch.setattr(
        supervisor,
        "request_restart",
        lambda revision: events.append(("restart", revision)),
    )
    connection = ExecutorConnection(
        cast(Any, client),
        hello_factory=cast(Any, lambda: None),
        execute=lambda command: {"status": "queued"},
        max_concurrent_commands=1,
    )
    command = ExecutorCommand(
        id=new_command_id(), op="ui.config.apply", args={"revision": "a" * 64}
    )
    await connection._run_command(command, inventory_completion=None)
    assert events == [("submit", True), ("restart", "a" * 64)]
    events.clear()
    connection = ExecutorConnection(
        cast(Any, client),
        hello_factory=cast(Any, lambda: None),
        execute=lambda command: (_ for _ in ()).throw(ValueError("bad config")),
        max_concurrent_commands=1,
    )
    await connection._run_command(command, inventory_completion=None)
    assert events == [("submit", False)]
    events.clear()
    # RPC transport succeeded, but validation returned a business-level error.
    connection = ExecutorConnection(
        cast(Any, client),
        hello_factory=cast(Any, lambda: None),
        execute=lambda command: {"error": "RevisionConflict", "status": 409},
        max_concurrent_commands=1,
    )
    await connection._run_command(command, inventory_completion=None)
    assert events == [("submit", True)]  # Never restart a rejected file.
