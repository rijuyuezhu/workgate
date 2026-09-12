import atexit
import itertools
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from workgate.config.settings import clear_settings_cache

_SYSTEM_TMP_ROOT = Path(tempfile.mkdtemp(prefix="workgate-tests-"))
_SYSTEM_TMP_SEQUENCE = itertools.count()
atexit.register(shutil.rmtree, _SYSTEM_TMP_ROOT, ignore_errors=True)


def _reset_managed_deferred_sequence(job_recovery) -> None:
    with job_recovery._MANAGED_DEFERRED_SEQUENCE_LOCK:
        job_recovery._MANAGED_DEFERRED_NEXT_SEQUENCE = None


@pytest.fixture(autouse=True)
def isolated_runtime_paths(monkeypatch, tmp_path):
    state_dir = tmp_path / ".workgate"
    runtime_dir = tmp_path / ".xdg-runtime"
    # Keep the process temp root isolated without nesting it under pytest's
    # already-long tmp_path. tmux appends its own socket suffix under
    # TMUX_TMPDIR and Unix-domain socket paths have a small fixed limit.
    system_tmp = _SYSTEM_TMP_ROOT / str(next(_SYSTEM_TMP_SEQUENCE))
    runtime_dir.mkdir()
    system_tmp.mkdir()
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    if sys.platform == "darwin":
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
    elif sys.platform.startswith("win"):
        monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setattr(tempfile, "tempdir", str(system_tmp))
    clear_settings_cache()
    from workgate.config.settings import get_settings
    from workgate.executor.tool_session import configure_tool_session_store
    from workgate.executor.tool_session.store import ToolSessionStore
    from workgate.persistence import FileStateStore
    from workgate.utils.path_policy import resolve_path_with_policy

    initial_settings = get_settings()
    state_store = FileStateStore(lambda: get_settings().state_dir)

    def test_path_resolver(
        path,
        *,
        must_exist=False,
        allow_missing_parent=True,
        follow_final_symlink=True,
    ):
        settings = get_settings()
        return resolve_path_with_policy(
            path,
            workspace_root=settings.workspace_root,
            allow_full_control=settings.allow_full_control,
            path_denylist=tuple(settings.path_denylist),
            must_exist=must_exist,
            allow_missing_parent=allow_missing_parent,
            follow_final_symlink=follow_final_symlink,
        )

    test_store = ToolSessionStore(
        state_store=state_store,
        path_resolver=test_path_resolver,
        workspace_root=initial_settings.workspace_root,
        allow_full_control=initial_settings.allow_full_control,
        path_denylist=tuple(initial_settings.path_denylist),
        max_session_snapshots=initial_settings.max_session_snapshots,
        max_session_snapshot_bytes=initial_settings.max_session_snapshot_bytes,
    )
    previous_tool_session_store = configure_tool_session_store(test_store)
    try:
        yield
    finally:
        configure_tool_session_store(previous_tool_session_store)
        clear_settings_cache()


@pytest.fixture
async def managed_jobs_runtime_owner():
    from workgate.jobs import recovery as job_recovery
    from workgate.jobs.managed import (
        ManagedJobsRuntime,
        configure_managed_jobs_runtime,
    )

    runtime = ManagedJobsRuntime()
    await runtime.start()
    previous = configure_managed_jobs_runtime(runtime)
    _reset_managed_deferred_sequence(job_recovery)
    try:
        yield runtime
    finally:
        try:
            await runtime.aclose()
        finally:
            configure_managed_jobs_runtime(previous)
            _reset_managed_deferred_sequence(job_recovery)
