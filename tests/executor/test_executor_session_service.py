import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.support.sessions import build_tool_session_store
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor.errors import ExecutorOperationFailure
from workgate.executor.runtime import build_executor_runtime
from workgate.executor.sessions import ExecutorSessionService
from workgate.executor.shell_service import ShellService
from workgate.executor.tool_session.store import (
    ToolSessionStore,
    UnknownAgentSessionError,
)


def _real_service(tmp_path: Path) -> ExecutorSessionService:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        default_workdir=workspace,
        state_dir=tmp_path / "state",
        agent_bridge_enabled=False,
    )
    store = build_tool_session_store(settings)
    config = resolve_executor_config(settings)
    return ExecutorSessionService(config, store, ShellService(config, store))


def _config(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return resolve_executor_config(
        Settings(
            default_workdir=workspace,
            state_dir=tmp_path / "state",
            agent_bridge_enabled=False,
        )
    )


def test_executor_runtime_freezes_default_anchor_but_allows_external_workdirs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "executor-workspace"
    workspace.mkdir()
    next_workdir = workspace / "next"
    next_workdir.mkdir()
    other_workspace = tmp_path / "mutated-legacy-workspace"
    other_workspace.mkdir()
    settings = Settings(
        default_workdir=workspace,
        state_dir=tmp_path / "state",
        agent_bridge_enabled=False,
    )
    runtime = build_executor_runtime(
        resolve_executor_config(settings), enable_control_connection=False
    )
    store = runtime.services.tool_session_store

    first_id = "sess_0000000000000000000001"
    created = store.create_session(session_id=first_id, workdir=workspace)
    assert Path(created.workdir) == workspace

    settings.default_workdir = other_workspace
    changed = store.change_session_workdir(first_id, next_workdir)
    assert Path(changed.workdir) == next_workdir

    second = store.create_session(
        session_id="sess_0000000000000000000002",
        workdir=other_workspace,
    )
    assert Path(second.workdir) == other_workspace


def test_reconnect_inventory_reads_job_protection_once_and_lookup_stays_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _real_service(tmp_path)
    store = service._store
    ids = ("sess_0000000000000000000001", "sess_0000000000000000000002")
    for session_id in ids:
        store.create_session(
            session_id=session_id, workdir=service._config.default_workdir
        )
    jobs_path = store.state_store.layout.jobs_store_path
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    jobs_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "job_id": "job_0000000000000000000001",
                        "session_id": ids[0],
                        "kind": "shell",
                        "status": "running",
                    }
                ]
            }
        )
    )
    calls = 0
    original = store._active_job_session_ids_locked

    def observed_protection() -> set[str] | None:
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(
        store, "_active_job_session_ids_locked", observed_protection
    )
    rows = {str(row.session_id): row for row in service.inventory()}
    assert calls == 1
    assert rows[ids[0]].has_active_jobs is True
    assert rows[ids[1]].has_active_jobs is False
    store.touch_session(ids[1])
    summary = service.lookup(ids[1])
    assert summary is not None
    assert summary.last_active_at == store.require_session(ids[1]).updated_at
    assert calls == 2  # Single-session lookup never reuses old inventory state.


@pytest.mark.parametrize(
    ("job_backing_shells", "expected_persistent_shells"),
    ((frozenset({"shell-backed-by-job"}), False), (frozenset(), True)),
)
def test_reconnect_inventory_keeps_nonjob_persistent_shell_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_backing_shells: frozenset[str],
    expected_persistent_shells: bool,
) -> None:
    service = _real_service(tmp_path)
    session_id = "sess_0000000000000000000001"
    service._store.create_session(
        session_id=session_id, workdir=service._config.default_workdir
    )
    service._store.register_persistent_shell(session_id, "shell-backed-by-job")
    monkeypatch.setattr(
        service._shell.jobs,
        "backing_shell_ids",
        lambda _ids: job_backing_shells,
    )
    rows = service.inventory()
    assert len(rows) == 1
    assert rows[0].has_persistent_shells is expected_persistent_shells


def test_reconnect_inventory_preserves_uncertain_job_fail_closed(
    tmp_path: Path,
) -> None:
    service = _real_service(tmp_path)
    store = service._store
    for index in (1, 2):
        store.create_session(
            session_id=f"sess_{index:022d}",
            workdir=service._config.default_workdir,
        )
    jobs_path = store.state_store.layout.jobs_store_path
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    jobs_path.write_text("{corrupted}")
    rows = service.inventory()
    assert len(rows) == 2
    assert all(row.has_active_jobs for row in rows)


def test_executor_session_lookup_absence_and_non_directory_workdir(
    tmp_path: Path,
) -> None:
    service = _real_service(tmp_path)

    assert service.lookup("sess_missing") is None

    file_path = service._config.default_workdir / "not-a-directory"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError, match="not-a-directory"):
        service._resolve_workdir("not-a-directory")


@pytest.mark.asyncio
async def test_executor_session_create_uses_resolve_default_workdir(
    tmp_path: Path,
) -> None:
    configured = tmp_path / "missing-default"
    config = resolve_executor_config(
        Settings(
            default_workdir=configured,
            state_dir=tmp_path / "state",
            agent_bridge_enabled=False,
        )
    )
    assert config.default_workdir == configured.resolve(strict=False)
    store = build_tool_session_store(
        Settings(
            default_workdir=configured,
            state_dir=tmp_path / "state",
            agent_bridge_enabled=False,
        )
    )
    service = ExecutorSessionService(config, store, ShellService(config, store))

    result = await service.create(
        "sess_0000000000000000000003",
        workdir=None,
        label=None,
    )

    root = str(Path("/").resolve(strict=False))
    assert result.workdir == root
    assert result.default_workdir == root


@pytest.mark.asyncio
async def test_executor_session_create_falls_back_when_default_disappears(
    tmp_path: Path,
) -> None:
    service = _real_service(tmp_path)
    service._config.default_workdir.rmdir()

    result = await service.create(
        "sess_0000000000000000000004",
        workdir=None,
        label=None,
    )

    root = str(Path("/").resolve(strict=False))
    assert result.workdir == root
    assert result.default_workdir == root


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
