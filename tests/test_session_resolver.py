import pytest

from tests.helpers import build_tool_session_store
from workgate.config.settings import Settings, clear_settings_cache
from workgate.executor.tool_session import store as store_module
from workgate.executor.tool_session.bindings import SessionBinding
from workgate.executor.tool_session.resolver import SessionResolver
from workgate.executor.tool_session.store import (
    SessionTerminationRequestedError,
)


def _store(tmp_path, monkeypatch) -> store_module.ToolSessionStore:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    store = build_tool_session_store(Settings())
    store.clear()
    return store


def _session_id(index: int) -> str:
    return f"sess_{index:022d}"


def test_admit_tool_sessions_checks_every_session_before_refreshing_any(
    tmp_path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    first = store.create_session(session_id=_session_id(1), workdir=tmp_path)
    second = store.create_session(session_id=_session_id(2), workdir=tmp_path)
    store.prepare_session_termination(second.session_id)
    first_before = store.require_session(first.session_id)

    with pytest.raises(SessionTerminationRequestedError) as exc_info:
        store.admit_tool_sessions((first.session_id, second.session_id))

    assert exc_info.value.session_id == second.session_id
    assert (
        store.require_session(first.session_id).updated_at
        == first_before.updated_at
    )


def test_touch_session_remains_mechanism_not_admission_authority(
    tmp_path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    session = store.create_session(session_id=_session_id(1), workdir=tmp_path)
    store.prepare_session_termination(session.session_id)

    touched = store.touch_session(session.session_id)

    assert touched.termination_requested_at is not None
    with pytest.raises(SessionTerminationRequestedError):
        store.admit_active_session(session.session_id)


def test_admit_active_session_does_not_reenter_touch_after_validation(
    tmp_path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    session = store.create_session(session_id=_session_id(1), workdir=tmp_path)
    original_touch = store.touch_session

    def terminate_before_touch(session_id: str):
        store.prepare_session_termination(session_id)
        return original_touch(session_id)

    monkeypatch.setattr(store, "touch_session", terminate_before_touch)

    admitted = store.admit_active_session(session.session_id)

    assert admitted.termination_requested_at is None
    assert (
        store.require_session(session.session_id).termination_requested_at
        is None
    )


def test_resolver_reads_fresh_workdir_for_each_operation(
    tmp_path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    first_workdir = tmp_path / "first"
    second_workdir = tmp_path / "second"
    first_workdir.mkdir()
    second_workdir.mkdir()
    session = store.create_session(
        session_id=_session_id(1), workdir=first_workdir
    )
    resolver = SessionResolver(store)

    first_binding = resolver.resolve_active_binding(session.session_id)
    store.change_session_workdir(session.session_id, second_workdir)
    second_binding = resolver.resolve_active_binding(session.session_id)

    assert first_binding == SessionBinding(
        session_id=session.session_id, workdir=str(first_workdir.resolve())
    )
    assert second_binding == SessionBinding(
        session_id=session.session_id, workdir=str(second_workdir.resolve())
    )
    assert first_binding is not second_binding


def test_resolver_cleanup_path_marks_termination_and_returns_binding(
    tmp_path, monkeypatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    session = store.create_session(session_id=_session_id(1), workdir=tmp_path)

    binding = SessionResolver(store).prepare_cleanup_binding(session.session_id)

    assert binding == SessionBinding(
        session_id=session.session_id, workdir=str(tmp_path.resolve())
    )
    prepared = store.require_session(session.session_id)
    assert prepared.termination_requested_at is not None
