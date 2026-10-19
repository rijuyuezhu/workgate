"""Executor-side read-only configuration RPC uses the real startup snapshot."""

from types import SimpleNamespace

import pytest

from workgate.config import inspection
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor.runtime import build_executor_runtime
from workgate.protocol.executor import ExecutorCommand
from workgate.protocol.ids import new_command_id, new_session_id


@pytest.mark.asyncio
async def test_config_inspect_protocol_has_no_session_or_arguments(
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
    result = {"role": "executor", "status": "running", "fields": []}
    observed = []

    def snapshot(role):
        observed.append(role)
        return SimpleNamespace(describe=lambda: result)

    monkeypatch.setattr(inspection, "current_startup_config", snapshot)
    command = ExecutorCommand(id=new_command_id(), op="ui.config.inspect")
    assert await runtime._execute_protocol_command_with_state(command) == {
        **result,
        "apply_status": "unmanaged",
    }
    assert observed == ["executor"]

    for forbidden in (
        ExecutorCommand(
            id=new_command_id(),
            op="ui.config.inspect",
            session_id=new_session_id(),
        ),
        ExecutorCommand(
            id=new_command_id(), op="ui.config.inspect", args={"extra": True}
        ),
    ):
        with pytest.raises(ValueError, match="cannot be session-bound"):
            await runtime._execute_protocol_command_with_state(forbidden)
    assert observed == ["executor"]  # Invalid commands never inspect state.

    monkeypatch.setattr(inspection, "current_startup_config", lambda role: None)
    with pytest.raises(
        RuntimeError, match="startup configuration is unavailable"
    ):
        await runtime._execute_protocol_command_with_state(command)


@pytest.mark.asyncio
async def test_config_save_rpc_rejects_wrong_args_and_returns_safe_conflict(
    tmp_path, monkeypatch
):
    from workgate.config import editing
    from workgate.config.editing import ConfigEditError

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
    calls = []

    def save(selected, *, revision, changes):
        calls.append((selected, revision, changes))
        return "b" * 64

    monkeypatch.setattr(editing, "save_config", save)
    args = {
        "revision": "a" * 64,
        "changes": {"max_jobs": {"op": "set", "value": 22}},
    }
    result = await runtime._execute_protocol_command_with_state(
        ExecutorCommand(id=new_command_id(), op="ui.config.save", args=args)
    )
    assert result == {"revision": "b" * 64}
    assert calls == [(snapshot, "a" * 64, args["changes"])]
    for wrong in (
        ExecutorCommand(id=new_command_id(), op="ui.config.save", args={}),
        ExecutorCommand(
            id=new_command_id(),
            op="ui.config.save",
            args={"revision": 22, "changes": {}},
        ),
        ExecutorCommand(
            id=new_command_id(),
            op="ui.config.save",
            args=args,
            session_id=new_session_id(),
        ),
    ):
        with pytest.raises(ValueError, match="Invalid config save request"):
            await runtime._execute_protocol_command_with_state(wrong)
    assert len(calls) == 1

    def conflict(*args, **kwargs):
        raise ConfigEditError("RevisionConflict", 409)

    monkeypatch.setattr(editing, "save_config", conflict)
    result = await runtime._execute_protocol_command_with_state(
        ExecutorCommand(id=new_command_id(), op="ui.config.save", args=args)
    )
    assert result == {"error": "RevisionConflict", "status": 409}
