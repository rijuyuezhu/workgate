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
    yield
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
