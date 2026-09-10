from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

import workgate.executor.shell_service as shell_service_module
from workgate.config.settings import Settings
from workgate.executor.config import resolve_executor_config
from workgate.executor.shell_service import ShellService
from workgate.schemas.result_models.shell import (
    KillPersistentShellOutput,
    ListPersistentShellsOutput,
    PersistentShellInfo,
)
from workgate.tool_session.store import ToolSessionStore


class _Store:
    def __init__(self) -> None:
        self.admitted: list[str] = []
        self.reconciled: list[tuple[str, set[str]]] = []

    def admit_active_session(self, session_id: str) -> None:
        self.admitted.append(session_id)

    def reconcile_session_persistent_shells(
        self, session_id: str, shell_ids: set[str]
    ) -> None:
        self.reconciled.append((session_id, shell_ids))


def _service(tmp_path: Path) -> tuple[ShellService, _Store]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = resolve_executor_config(
        Settings(
            workspace_root=workspace,
            state_dir=tmp_path / "state",
            remote_enabled=False,
            agent_bridge_enabled=False,
        )
    )
    store = _Store()
    return ShellService(config, cast(ToolSessionStore, store)), store


@pytest.mark.asyncio
async def test_shell_service_forwards_command_and_start_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _store = _service(tmp_path)
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def bash_execute(*args: Any, **kwargs: Any) -> str:
        calls.append(("bash", args, kwargs))
        return "bash-result"

    async def python_execute(*args: Any, **kwargs: Any) -> str:
        calls.append(("python", args, kwargs))
        return "python-result"

    async def start_execute(*args: Any, **kwargs: Any) -> str:
        calls.append(("start", args, kwargs))
        return "start-result"

    monkeypatch.setattr(shell_service_module, "bash_execute", bash_execute)
    monkeypatch.setattr(
        shell_service_module, "run_python_code_execute", python_execute
    )
    monkeypatch.setattr(
        shell_service_module, "start_persistent_shell_execute", start_execute
    )

    assert (
        await service.bash(
            {
                "session_id": "sess-1",
                "command": "echo hi",
                "cwd": "subdir",
                "timeout_s": 3,
                "max_output_bytes": 99,
                "env": {"A": "B"},
                "async_": True,
                "pty": False,
                "name": "job",
            }
        )
        == "bash-result"
    )
    assert (
        await service.run_python_code(
            {
                "session_id": "sess-1",
                "code": "print(1)",
                "pty": True,
            }
        )
        == "python-result"
    )
    assert (
        await service.start(
            {
                "session_id": "sess-1",
                "cwd": "subdir",
                "name": "shell",
                "command": "python",
            }
        )
        == "start-result"
    )

    assert [name for name, _args, _kwargs in calls] == [
        "bash",
        "python",
        "start",
    ]
    assert calls[0][1][2:5] == ("sess-1", "echo hi", "subdir")
    assert calls[0][2]["job_start"] == service.jobs.start
    assert calls[1][1][2:5] == ("sess-1", "print(1)", ".")
    assert calls[2][2]["owner_session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_shell_service_enforces_owned_shell_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store = _service(tmp_path)
    calls: list[tuple[Any, ...]] = []

    async def owned(*_args: Any) -> list[str]:
        return ["shell-1"]

    async def sent(*args: Any) -> str:
        calls.append(("send", *args[1:]))
        return "sent"

    async def resized(*args: Any) -> str:
        calls.append(("resize", *args[1:]))
        return "resized"

    async def read(*args: Any, **kwargs: Any) -> str:
        calls.append(("read", *args[1:], kwargs))
        return "read"

    async def killed(*args: Any) -> str:
        calls.append(("kill", *args[2:]))
        return "killed"

    monkeypatch.setattr(
        shell_service_module, "list_owned_persistent_shell_ids_execute", owned
    )
    monkeypatch.setattr(
        shell_service_module, "send_persistent_shell_input_execute", sent
    )
    monkeypatch.setattr(
        shell_service_module, "resize_persistent_shell_execute", resized
    )
    monkeypatch.setattr(
        shell_service_module, "read_persistent_shell_output_execute", read
    )
    monkeypatch.setattr(
        shell_service_module, "kill_persistent_shell_execute", killed
    )

    assert (
        await service.send(
            {
                "session_id": "sess-1",
                "shell_id": "shell-1",
                "input_text": "pwd",
                "enter": False,
            }
        )
        == "sent"
    )
    assert (
        await service.resize(
            {
                "session_id": "sess-1",
                "shell_id": "shell-1",
                "cols": 80,
                "rows": 24,
            }
        )
        == "resized"
    )
    assert (
        await service.read(
            {
                "session_id": "sess-1",
                "shell_id": "shell-1",
                "lines": 12,
                "preserve_ansi": True,
            }
        )
        == "read"
    )
    assert (
        await service.kill({"session_id": "sess-1", "shell_id": "shell-1"})
        == "killed"
    )

    assert store.admitted == ["sess-1"] * 4
    assert calls[0] == ("send", "shell-1", "pwd", False)
    assert calls[1] == ("resize", "shell-1", 80, 24)
    assert calls[2] == ("read", "shell-1", 12, {"preserve_ansi": True})
    assert calls[3] == ("kill", "shell-1")

    with pytest.raises(ValueError, match="preserve_ansi must be a boolean"):
        await service.read(
            {
                "session_id": "sess-1",
                "shell_id": "shell-1",
                "preserve_ansi": "yes",
            }
        )


@pytest.mark.asyncio
async def test_shell_service_rejects_uncertain_and_foreign_shell_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _store = _service(tmp_path)

    async def uncertain(*_args: Any) -> None:
        return None

    monkeypatch.setattr(
        shell_service_module,
        "list_owned_persistent_shell_ids_execute",
        uncertain,
    )
    with pytest.raises(RuntimeError, match="ownership is currently uncertain"):
        await service.send(
            {"session_id": "sess-1", "shell_id": "shell-1", "input_text": "x"}
        )

    async def none_owned(*_args: Any) -> list[str]:
        return []

    monkeypatch.setattr(
        shell_service_module,
        "list_owned_persistent_shell_ids_execute",
        none_owned,
    )
    with pytest.raises(ValueError, match="is not owned by session"):
        await service.send(
            {"session_id": "sess-1", "shell_id": "shell-1", "input_text": "x"}
        )


@pytest.mark.asyncio
async def test_shell_service_lists_owned_and_routes_ui_unowned_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store = _service(tmp_path)
    inventory = ListPersistentShellsOutput(
        shells=[
            PersistentShellInfo(shell_id="shell-1"),
            PersistentShellInfo(shell_id="shell-other"),
        ]
    )
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def owned(*_args: Any) -> list[str]:
        return ["shell-1"]

    async def listed(*_args: Any) -> ListPersistentShellsOutput:
        return inventory

    async def record(name: str, *args: Any, **kwargs: Any) -> str:
        calls.append((name, args, kwargs))
        return name

    monkeypatch.setattr(
        shell_service_module, "list_owned_persistent_shell_ids_execute", owned
    )
    monkeypatch.setattr(
        shell_service_module, "list_persistent_shells_execute", listed
    )
    monkeypatch.setattr(
        shell_service_module,
        "start_persistent_shell_execute",
        lambda *args, **kwargs: record("start", *args, **kwargs),
    )
    monkeypatch.setattr(
        shell_service_module,
        "send_persistent_shell_input_execute",
        lambda *args, **kwargs: record("send", *args, **kwargs),
    )
    monkeypatch.setattr(
        shell_service_module,
        "resize_persistent_shell_execute",
        lambda *args, **kwargs: record("resize", *args, **kwargs),
    )
    monkeypatch.setattr(
        shell_service_module,
        "read_persistent_shell_output_execute",
        lambda *args, **kwargs: record("read", *args, **kwargs),
    )
    monkeypatch.setattr(
        shell_service_module,
        "kill_persistent_shell_execute",
        lambda *args, **kwargs: record("kill", *args, **kwargs),
    )

    visible = await service.list({"session_id": "sess-1"})
    assert [shell.shell_id for shell in visible.shells] == ["shell-1"]
    assert await service.list_all() == inventory
    assert store.admitted == ["sess-1"]

    assert await service.start_unowned({"cwd": ".", "name": "ui"}) == "start"
    assert (
        await service.send_unowned(
            {"shell_id": "shell-1", "input_text": "x", "enter": False}
        )
        == "send"
    )
    assert (
        await service.resize_unowned(
            {"shell_id": "shell-1", "cols": 100, "rows": 40}
        )
        == "resize"
    )
    assert (
        await service.read_unowned({"shell_id": "shell-1", "lines": 9})
        == "read"
    )
    assert await service.kill_unowned({"shell_id": "shell-1"}) == "kill"
    assert [name for name, _args, _kwargs in calls] == [
        "start",
        "send",
        "resize",
        "read",
        "kill",
    ]


@pytest.mark.asyncio
async def test_shell_service_stop_owned_reconciles_and_stops_live_shells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store = _service(tmp_path)
    inventories = iter(
        [
            ["shell-1", "shell-2"],
            ["shell-1", "shell-2"],
            ["shell-1", "shell-2"],
        ]
    )
    killed: list[str] = []

    async def owned(*_args: Any) -> list[str]:
        return next(inventories)

    async def kill(
        _config: Any, _store: Any, shell_id: str
    ) -> KillPersistentShellOutput:
        killed.append(shell_id)
        return KillPersistentShellOutput(
            shell_id=shell_id, killed=True, backend="tmux"
        )

    monkeypatch.setattr(
        shell_service_module, "list_owned_persistent_shell_ids_execute", owned
    )
    monkeypatch.setattr(
        shell_service_module, "kill_persistent_shell_execute", kill
    )

    assert await service.stop_owned("sess-1") == ["shell-1", "shell-2"]
    assert killed == ["shell-1", "shell-2"]
    assert store.reconciled == [("sess-1", {"shell-1", "shell-2"})]


@pytest.mark.asyncio
async def test_shell_service_stop_owned_fails_closed_when_ownership_is_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _store = _service(tmp_path)

    async def uncertain(*_args: Any) -> None:
        return None

    monkeypatch.setattr(
        shell_service_module,
        "list_owned_persistent_shell_ids_execute",
        uncertain,
    )

    with pytest.raises(RuntimeError, match="ownership could not be determined"):
        await service.stop_owned("sess-1")


@pytest.mark.asyncio
async def test_shell_service_stop_owned_rejects_still_live_failed_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _store = _service(tmp_path)
    inventories = iter([["shell-1"], ["shell-1"]])

    async def owned(*_args: Any) -> list[str]:
        return next(inventories)

    async def failed_kill(
        _config: Any, _store: Any, shell_id: str
    ) -> KillPersistentShellOutput:
        return KillPersistentShellOutput(
            shell_id=shell_id,
            killed=False,
            stderr="backend refused",
            backend="tmux",
        )

    async def active(*_args: Any) -> set[str]:
        return {"shell-1"}

    monkeypatch.setattr(
        shell_service_module, "list_owned_persistent_shell_ids_execute", owned
    )
    monkeypatch.setattr(
        shell_service_module, "kill_persistent_shell_execute", failed_kill
    )
    monkeypatch.setattr(
        shell_service_module,
        "authoritative_persistent_shell_ids_execute",
        active,
    )

    with pytest.raises(RuntimeError, match="backend refused"):
        await service.stop_owned("sess-1")
