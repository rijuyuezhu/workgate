from pathlib import Path

import pytest

from workgate.config.settings import Settings
from workgate.executor.config import resolve_executor_config
from workgate.executor.tool_session.snapshots import SnapshotRepository
from workgate.executor.tool_session.store import (
    SessionTerminationRequestedError,
    ToolSessionStore,
    UnknownAgentSessionError,
)
from workgate.persistence import FileStateStore


def _session_id(index: int) -> str:
    return f"sess_{index:022d}"


def _store(
    tmp_path: Path,
    **settings_overrides,
) -> tuple[ToolSessionStore, Settings]:
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / ".state",
        agent_bridge_enabled=False,
        **settings_overrides,
    )
    store = _new_store(settings)
    store.clear()
    return store, settings


def _new_store(settings: Settings) -> ToolSessionStore:
    config = resolve_executor_config(settings)
    return ToolSessionStore(
        state_store=FileStateStore(lambda: config.state_dir),
        workspace_root=config.workspace_root,
        allow_full_control=config.allow_full_control,
        path_denylist=config.path_denylist,
        max_session_snapshots=config.max_session_snapshots,
        max_session_snapshot_bytes=config.max_session_snapshot_bytes,
    )


def _create(
    store: ToolSessionStore,
    workdir: str | Path,
    *,
    index: int = 1,
    label: str | None = None,
):
    return store.create_session(
        session_id=_session_id(index),
        workdir=workdir,
        label=label,
    )


def _snapshot(store: ToolSessionStore, session_id: str, marker: str):
    return store.record_file_snapshot(
        session_id=session_id,
        path=f"{marker}.txt",
        file_sha256=(marker.encode("utf-8").hex() * 64)[:64],
        total_lines=3,
        seen_ranges=((1, 2),),
    )


def test_create_session_requires_control_allocated_shared_id(
    tmp_path: Path,
) -> None:
    store, _settings = _store(tmp_path)

    with pytest.raises(ValueError, match="session_id is invalid"):
        store.create_session(session_id="ABC12345", workdir=tmp_path)

    session = _create(store, tmp_path, index=1, label="shared")
    assert session.session_id == _session_id(1)
    assert session.workdir == str(tmp_path.resolve())
    assert session.label == "shared"

    with pytest.raises(ValueError, match="already exists"):
        _create(store, tmp_path, index=1)


def test_session_workdirs_must_be_directories(tmp_path: Path) -> None:
    store, _settings = _store(tmp_path)
    not_a_directory = tmp_path / "file.txt"
    not_a_directory.write_text("x", encoding="utf-8")

    with pytest.raises(NotADirectoryError):
        _create(store, not_a_directory, index=1)

    session = _create(store, tmp_path, index=2)
    with pytest.raises(NotADirectoryError):
        store.change_session_workdir(session.session_id, not_a_directory)


def test_require_session_rejects_unknown_shared_id(tmp_path: Path) -> None:
    store, _settings = _store(tmp_path)

    with pytest.raises(UnknownAgentSessionError, match="session_start"):
        store.require_session(_session_id(99))


def test_sessions_and_snapshots_survive_cold_store_instance(
    tmp_path: Path,
) -> None:
    store, settings = _store(tmp_path)
    session = _create(store, tmp_path, index=1, label="durable")
    snapshot = _snapshot(store, session.session_id, "durable")

    cold = _new_store(settings)

    restored = cold.require_session(session.session_id)
    assert restored.workdir == str(tmp_path.resolve())
    assert restored.label == "durable"
    assert (
        cold.get_snapshot(session.session_id, snapshot.snapshot_id) == snapshot
    )


def test_snapshots_are_isolated_by_shared_session(tmp_path: Path) -> None:
    store, _settings = _store(tmp_path)
    first = _create(store, tmp_path, index=1)
    second = _create(store, tmp_path, index=2)
    snapshot = _snapshot(store, first.session_id, "first")

    assert (
        store.get_snapshot(first.session_id, snapshot.snapshot_id) == snapshot
    )
    assert store.get_snapshot(second.session_id, snapshot.snapshot_id) is None


def test_change_session_workdir_updates_session_and_clears_grounding(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store, _settings = _store(tmp_path)
    session = _create(store, first, index=1)
    snapshot = _snapshot(store, session.session_id, "before")

    updated = store.change_session_workdir(session.session_id, second)

    assert updated.workdir == str(second.resolve())
    assert store.require_session(session.session_id).workdir == str(
        second.resolve()
    )
    assert store.get_snapshot(session.session_id, snapshot.snapshot_id) is None


def test_change_workdir_failure_before_snapshot_invalidation_keeps_old_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store, settings = _store(tmp_path)
    session = _create(store, first, index=1)
    snapshot = _snapshot(store, session.session_id, "before")

    def fail_remove(_session_id: str) -> None:
        raise OSError("snapshot invalidation failed")

    monkeypatch.setattr(
        store._snapshot_repository, "remove_session", fail_remove
    )
    with pytest.raises(OSError, match="snapshot invalidation failed"):
        store.change_session_workdir(session.session_id, second)

    cold = _new_store(settings)
    assert cold.require_session(session.session_id).workdir == str(
        first.resolve()
    )
    assert (
        cold.get_snapshot(session.session_id, snapshot.snapshot_id) == snapshot
    )


def test_change_workdir_failure_after_snapshot_invalidation_keeps_old_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store, settings = _store(tmp_path)
    session = _create(store, first, index=1)
    snapshot = _snapshot(store, session.session_id, "before")

    original_write = store._write_session_locked

    def fail_updated_write(candidate) -> None:
        if candidate.workdir == str(second.resolve()):
            raise OSError("metadata write failed")
        original_write(candidate)

    monkeypatch.setattr(store, "_write_session_locked", fail_updated_write)
    with pytest.raises(OSError, match="metadata write failed"):
        store.change_session_workdir(session.session_id, second)

    cold = _new_store(settings)
    assert cold.require_session(session.session_id).workdir == str(
        first.resolve()
    )
    assert cold.get_snapshot(session.session_id, snapshot.snapshot_id) is None


def test_termination_is_durable_idempotent_and_blocks_new_tool_work(
    tmp_path: Path,
) -> None:
    store, settings = _store(tmp_path)
    session = _create(store, tmp_path, index=1)

    first = store.request_termination(session.session_id)
    second = store.request_termination(session.session_id)
    assert first.termination_requested_at is not None
    assert second.termination_requested_at == first.termination_requested_at

    with pytest.raises(SessionTerminationRequestedError):
        store.admit_active_session(session.session_id)

    cleanup = store.require_cleanup_sessions((session.session_id,))
    assert cleanup[0].session_id == session.session_id

    cold = _new_store(settings)
    with pytest.raises(SessionTerminationRequestedError):
        cold.admit_active_session(session.session_id)


def test_multi_session_admission_validates_all_before_refreshing_any(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _settings = _store(tmp_path)
    first = _create(store, tmp_path, index=1)
    second = _create(store, tmp_path, index=2)
    clock = [100.0]
    monkeypatch.setattr(
        "workgate.executor.tool_session.store.time.time", lambda: clock[0]
    )
    first = store.touch_session(first.session_id)
    second = store.touch_session(second.session_id)
    store.request_termination(second.session_id)
    before = store.require_session(first.session_id).updated_at
    clock[0] = 200.0

    with pytest.raises(SessionTerminationRequestedError):
        store.admit_tool_sessions((first.session_id, second.session_id))

    assert store.require_session(first.session_id).updated_at == before


def test_end_session_removes_metadata_and_grounding(tmp_path: Path) -> None:
    store, _settings = _store(tmp_path)
    session = _create(store, tmp_path, index=1)
    snapshot = _snapshot(store, session.session_id, "owned")

    ended = store.end_session(session.session_id)
    assert ended.session_id == session.session_id
    with pytest.raises(UnknownAgentSessionError):
        store.require_session(session.session_id)
    with pytest.raises(UnknownAgentSessionError):
        store.get_snapshot(session.session_id, snapshot.snapshot_id)


def test_list_sessions_orders_by_latest_activity(
    tmp_path: Path, monkeypatch
) -> None:
    store, _settings = _store(tmp_path)
    clock = [100.0]
    monkeypatch.setattr(
        "workgate.executor.tool_session.store.time.time", lambda: clock[0]
    )
    first = _create(store, tmp_path, index=1)
    clock[0] = 101.0
    second = _create(store, tmp_path, index=2)
    clock[0] = 102.0
    store.touch_session(first.session_id)

    assert [row.session_id for row in store.list_sessions()] == [
        first.session_id,
        second.session_id,
    ]


def test_snapshot_count_retention_keeps_newest_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _settings = _store(tmp_path, max_session_snapshots=2)
    session = _create(store, tmp_path, index=1)
    clock = [100.0]
    monkeypatch.setattr(
        "workgate.executor.tool_session.store.time.time", lambda: clock[0]
    )

    first = _snapshot(store, session.session_id, "one")
    clock[0] += 1
    second = _snapshot(store, session.session_id, "two")
    clock[0] += 1
    third = _snapshot(store, session.session_id, "three")

    assert store.get_snapshot(session.session_id, first.snapshot_id) is None
    assert store.get_snapshot(session.session_id, second.snapshot_id) == second
    assert store.get_snapshot(session.session_id, third.snapshot_id) == third


def test_snapshot_metadata_rejects_record_over_byte_limit(
    tmp_path: Path,
) -> None:
    store, _settings = _store(tmp_path, max_session_snapshot_bytes=1024)
    session = _create(store, tmp_path, index=1)

    with pytest.raises(ValueError, match="max_session_snapshot_bytes"):
        store.record_file_snapshot(
            session_id=session.session_id,
            path="x" * 2000,
            file_sha256="a" * 64,
            total_lines=1,
            seen_ranges=((1, 1),),
        )


def test_snapshot_repository_rejects_invalid_or_cross_session_payloads(
    tmp_path: Path,
) -> None:
    state_store = FileStateStore(lambda: tmp_path / ".state")
    repository = SnapshotRepository(
        state_store, max_snapshots=2, max_bytes=4096
    )
    session_id = _session_id(1)
    path = state_store.layout.session_snapshots_path(session_id)

    state_store.write_json(path, {"snapshots": "invalid"})
    with pytest.raises(ValueError, match="snapshots array"):
        repository.get(session_id, "snap-1")

    state_store.write_json(
        path,
        {
            "snapshots": [
                {
                    "session_id": _session_id(2),
                    "snapshot_id": "snap-1",
                    "path": "file.txt",
                    "file_sha256": "a" * 64,
                    "total_lines": 1,
                    "seen_ranges": [[1, 1]],
                    "created_at": 1.0,
                    "sequence": 1,
                }
            ]
        },
    )
    with pytest.raises(ValueError, match="owner does not match"):
        repository.get(session_id, "snap-1")


def test_snapshot_repository_zero_retention_removes_empty_index(
    tmp_path: Path,
) -> None:
    state_store = FileStateStore(lambda: tmp_path / ".state")
    repository = SnapshotRepository(
        state_store, max_snapshots=0, max_bytes=4096
    )
    session_id = _session_id(1)

    repository.record(
        session_id=session_id,
        snapshot_id="snap-1",
        path="file.txt",
        file_sha256="a" * 64,
        total_lines=1,
        seen_ranges=((1, 1),),
        created_at=1.0,
    )

    assert repository.get(session_id, "snap-1") is None
    assert not state_store.layout.session_snapshots_path(session_id).exists()


def test_persistent_shell_registry_releases_and_reconciles(
    tmp_path: Path,
) -> None:
    store, _settings = _store(tmp_path)
    first = _create(store, tmp_path, index=1)
    second = _create(store, tmp_path, index=2)

    store.register_persistent_shell(first.session_id, "shell-one")
    store.register_persistent_shell(first.session_id, "shell-two")
    store.register_persistent_shell(second.session_id, "shell-three")
    assert store.persistent_shell_ids() == {
        "shell-one",
        "shell-two",
        "shell-three",
    }

    store.release_session_persistent_shell(first.session_id, "shell-one")
    store.reconcile_session_persistent_shells(first.session_id, {"shell-two"})
    store.reconcile_persistent_shells({"shell-two", "shell-three"})
    assert store.require_session(first.session_id).persistent_shell_ids == (
        "shell-two",
    )
    assert store.require_session(second.session_id).persistent_shell_ids == (
        "shell-three",
    )

    store.release_persistent_shell("shell-two")
    assert store.persistent_shell_ids() == {"shell-three"}


def test_persistent_shell_release_noops_are_stable(tmp_path: Path) -> None:
    store, _settings = _store(tmp_path)
    session = _create(store, tmp_path, index=1)

    unchanged = store.release_session_persistent_shell(
        session.session_id, "missing"
    )
    assert unchanged == session
    store.release_persistent_shell("   ")
    assert store.persistent_shell_ids() == set()


def test_tool_call_allowed_facade_selects_active_or_cleanup_admission(
    tmp_path: Path,
) -> None:
    store, _settings = _store(tmp_path)
    session = _create(store, tmp_path, index=1)

    store.assert_tool_call_allowed((session.session_id,))
    store.request_termination(session.session_id)
    store.assert_tool_call_allowed(
        (session.session_id,), termination_cleanup=True
    )
    with pytest.raises(SessionTerminationRequestedError):
        store.assert_tool_call_allowed((session.session_id,))


def test_exclusive_shell_reservation_rejects_other_session_owner(
    tmp_path: Path,
) -> None:
    store, _settings = _store(tmp_path)
    first = _create(store, tmp_path, index=1)
    second = _create(store, tmp_path, index=2)

    assert store.reserve_persistent_shell(
        first.session_id, "shared", exclusive=True
    )
    assert not store.reserve_persistent_shell(
        first.session_id, "shared", exclusive=True
    )
    with pytest.raises(RuntimeError, match="already reserved"):
        store.reserve_persistent_shell(
            second.session_id, "shared", exclusive=True
        )


def test_scoped_shell_reconciliation_preserves_other_session(
    tmp_path: Path,
) -> None:
    store, _settings = _store(tmp_path)
    first = _create(store, tmp_path, index=1)
    second = _create(store, tmp_path, index=2)
    store.register_persistent_shell(first.session_id, "one")
    store.register_persistent_shell(second.session_id, "two")

    store.reconcile_session_persistent_shells(first.session_id, set())

    assert store.require_session(first.session_id).persistent_shell_ids == ()
    assert store.require_session(second.session_id).persistent_shell_ids == (
        "two",
    )


def test_cleanup_metadata_reports_activity_and_resource_ownership(
    tmp_path: Path,
) -> None:
    store, _settings = _store(tmp_path)
    session = _create(store, tmp_path, index=1)
    store.register_persistent_shell(session.session_id, "shell")

    updated_at, has_shells, has_jobs = store.session_cleanup_metadata(
        session.session_id
    )

    assert updated_at == store.require_session(session.session_id).updated_at
    assert has_shells is True
    assert has_jobs is False


def test_resolve_session_path_uses_store_owned_workspace_policy(
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    target = workdir / "file.txt"
    target.write_text("ok", encoding="utf-8")
    store, _settings = _store(tmp_path)
    session = _create(store, workdir, index=1)

    assert (
        store.resolve_session_path(session, "file.txt", must_exist=True)
        == target
    )
    with pytest.raises(ValueError, match="escapes session workdir"):
        store.resolve_session_path(session, "../outside.txt")
