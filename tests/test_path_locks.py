from pathlib import Path

import workgate.utils.path_locks as path_lock_module
from workgate.config.settings import clear_settings_cache
from workgate.utils.path_locks import path_lock, path_locks


def _configure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()


def test_path_lock_registry_releases_unused_entries(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)

    for index in range(500):
        with path_lock(tmp_path / f"file-{index}"):
            pass

    assert path_lock_module._PATH_LOCKS == {}
    lock_files = list((tmp_path / ".state" / "locks").glob("*.lock"))
    assert len(lock_files) <= 256


def test_multi_path_lock_deduplicates_and_orders_paths(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    first = tmp_path / "a"
    second = tmp_path / "b"

    with path_locks([second, first, second]):
        assert len(path_lock_module._PATH_LOCKS) == 2

    assert path_lock_module._PATH_LOCKS == {}
