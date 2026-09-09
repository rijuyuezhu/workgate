from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from workgate.config.settings import Settings
from workgate.executor.config import resolve_executor_config
from workgate.executor.errors import ExecutorOperationFailure
from workgate.executor.runtime import build_executor_runtime
from workgate.executor.sessions import ExecutorSessionService
from workgate.executor.shell_service import ShellService
from workgate.persistence import FileStateStore
from workgate.tool_session.store import (
    ToolSessionStore,
    UnknownAgentSessionError,
)


def _real_service(tmp_path: Path) -> ExecutorSessionService:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        workspace_root=workspace,
        state_dir=tmp_path / "state",
        remote_enabled=False,
        agent_bridge_enabled=False,
    )
    store = ToolSessionStore(
        FileStateStore(lambda: settings.state_dir),
        settings_provider=lambda: settings,
    )
    config = resolve_executor_config(settings)
    return ExecutorSessionService(config, store, ShellService(config, store))


def _config(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return resolve_executor_config(
        Settings(
            workspace_root=workspace,
            state_dir=tmp_path / "state",
            remote_enabled=False,
            agent_bridge_enabled=False,
        )
    )


def test_executor_runtime_freezes_session_workspace_authority(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "executor-workspace"
    workspace.mkdir()
    next_workdir = workspace / "next"
    next_workdir.mkdir()
    other_workspace = tmp_path / "mutated-legacy-workspace"
    other_workspace.mkdir()
    settings = Settings(
        workspace_root=workspace,
        state_dir=tmp_path / "state",
        remote_enabled=False,
        agent_bridge_enabled=False,
    )
    runtime = build_executor_runtime(settings, enable_control_connection=False)
    store = runtime.services.tool_session_store

    first_id = "sess_0000000000000000000001"
    created = store.create_session(session_id=first_id, workdir=workspace)
    assert Path(created.workdir) == workspace

    settings.workspace_root = other_workspace
    changed = store.change_session_workdir(first_id, next_workdir)
    assert Path(changed.workdir) == next_workdir

    with pytest.raises(ValueError, match="escapes workspace"):
        store.create_session(
            session_id="sess_0000000000000000000002",
            workdir=other_workspace,
        )


def test_executor_session_lookup_absence_and_non_directory_workdir(
    tmp_path: Path,
) -> None:
    service = _real_service(tmp_path)

    assert service.lookup("sess_missing") is None

    file_path = service._config.workspace_root / "not-a-directory"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError, match="not-a-directory"):
        service._resolve_workdir("not-a-directory")


@pytest.mark.asyncio
async def test_executor_session_terminate_is_idempotent_when_already_absent(
    tmp_path: Path,
) -> None:
    service = _real_service(tmp_path)

    assert await service.terminate("sess_missing") == {
        "session_id": "sess_missing",
        "absent": True,
    }


@pytest.mark.asyncio
async def test_executor_session_create_reports_confirmed_absence(
    tmp_path: Path,
) -> None:
    class Store:
        def create_session(self, **_kwargs: Any):
            raise OSError("disk full")

        def require_session(self, _session_id: str):
            raise UnknownAgentSessionError("missing")

    service = ExecutorSessionService(
        _config(tmp_path),
        cast(ToolSessionStore, Store()),
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    with pytest.raises(ExecutorOperationFailure) as raised:
        await service.create("sess_failed", workdir=".", label=None)

    assert raised.value.code == "session_create_absent"
    assert str(raised.value) == "disk full"


@pytest.mark.asyncio
async def test_executor_session_create_reports_unconfirmed_when_rollback_fails(
    tmp_path: Path,
) -> None:
    class Store:
        def create_session(self, **_kwargs: Any):
            raise OSError("create failed")

        def require_session(self, session_id: str):
            return SimpleNamespace(session_id=session_id)

        def end_session(self, _session_id: str):
            raise OSError("cleanup failed")

    service = ExecutorSessionService(
        _config(tmp_path),
        cast(ToolSessionStore, Store()),
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    with pytest.raises(ExecutorOperationFailure) as raised:
        await service.create("sess_uncertain", workdir=".", label="label")

    assert raised.value.code == "session_create_unconfirmed"
    assert "could not confirm absence" in str(raised.value)


def test_executor_session_failed_create_confirms_absence_after_rollback(
    tmp_path: Path,
) -> None:
    class Store:
        def __init__(self) -> None:
            self.present = True

        def require_session(self, session_id: str):
            if not self.present:
                raise UnknownAgentSessionError(session_id)
            return SimpleNamespace(session_id=session_id)

        def end_session(self, _session_id: str):
            self.present = False

    store = Store()
    service = ExecutorSessionService(
        _config(tmp_path),
        cast(ToolSessionStore, store),
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert service._confirm_absent_after_failed_create("sess_rollback") is True
    assert store.present is False
