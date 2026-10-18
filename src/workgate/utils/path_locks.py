"""Bounded thread/process locks for filesystem mutation paths."""

import hashlib
import threading
from collections.abc import Generator, Iterable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..persistence import get_state_store
from ..platform.path import path_lock_key
from .private_files import private_file_lock


@dataclass
class _ThreadPathLock:
    """One ref-counted in-process path lock."""

    lock: threading.RLock
    """Reentrant lock serializing one normalized path."""

    users: int = 0
    """Current holders and waiters referencing this registry entry."""


_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, _ThreadPathLock] = {}


def _thread_lock_for(path: Path) -> tuple[str, _ThreadPathLock]:
    key = path_lock_key(path)
    with _PATH_LOCKS_GUARD:
        entry = _PATH_LOCKS.get(key)
        if entry is None:
            entry = _ThreadPathLock(threading.RLock())
            _PATH_LOCKS[key] = entry
        entry.users += 1
        return key, entry


def _release_thread_lock(key: str, entry: _ThreadPathLock) -> None:
    with _PATH_LOCKS_GUARD:
        entry.users -= 1
        if entry.users <= 0 and _PATH_LOCKS.get(key) is entry:
            _PATH_LOCKS.pop(key, None)


def _lock_shard_path(path: Path) -> Path:
    directory = get_state_store().layout.locks_dir
    from ..app_paths import ensure_private_directory

    ensure_private_directory(directory)
    digest = hashlib.sha256(path_lock_key(path).encode("utf-8")).hexdigest()
    return directory / f"shard-{digest[:2]}.lock"


@contextmanager
def path_locks(paths: Iterable[Path]) -> Generator[None]:
    """Serialize mutations for normalized paths across threads and processes."""
    unique = [
        path
        for _key, path in sorted(
            {path_lock_key(path): path for path in paths}.items()
        )
    ]
    entries: list[tuple[str, _ThreadPathLock]] = []
    try:
        for path in unique:
            entries.append(_thread_lock_for(path))
        with ExitStack() as stack:
            for _key, entry in entries:
                stack.enter_context(entry.lock)
            shards = sorted(
                {_lock_shard_path(path) for path in unique}, key=str
            )
            for shard in shards:
                stack.enter_context(private_file_lock(shard))
            yield
    finally:
        for key, entry in reversed(entries):
            _release_thread_lock(key, entry)


@contextmanager
def path_lock(path: Path) -> Generator[None]:
    """Serialize one mutation through the shared multi-path lock implementation."""
    with path_locks([path]):
        yield
