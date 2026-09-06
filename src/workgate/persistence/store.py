"""Shared filesystem-backed persistence for private runtime state."""

import contextlib
import hashlib
import json
import re
import shutil
import threading
from collections.abc import Callable, Generator, Iterable
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..utils.private_files import (
    atomic_write_private_text,
    private_file_lock,
)

_STATE_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")


class StateStore(Protocol):
    """Minimal storage contract used by durable runtime-state repositories."""

    @property
    def layout(self) -> StateLayout:
        """Return the current configured state layout."""
        ...

    def read_json(
        self, path: Path, *, max_bytes: int | None = None
    ) -> Any | None:
        """Read one private JSON file, returning None when it is absent."""
        ...

    def write_json(self, path: Path, value: Any) -> None:
        """Atomically replace one private JSON file."""
        ...

    def remove(self, path: Path, *, recursive: bool = False) -> None:
        """Remove one private state path when it exists."""
        ...

    def iter_directories(self, path: Path) -> Iterable[Path]:
        """Return regular child directories in stable name order."""
        ...

    def transaction(self, path: Path) -> AbstractContextManager[None]:
        """Serialize one state-file transaction across threads and processes."""
        ...


@dataclass(frozen=True)
class StateLayout:
    """Canonical directory layout below one configured state root."""

    root: Path
    """Configured private runtime-state root."""

    @staticmethod
    def _component(value: str, *, field: str) -> str:
        normalized = str(value or "")
        if not _STATE_COMPONENT_RE.fullmatch(normalized):
            raise ValueError(f"invalid {field}: {value!r}")
        return normalized

    @property
    def sessions_dir(self) -> Path:
        """Directory containing one private folder per explicit agent session."""
        return self.root / "sessions"

    def session_dir(self, session_id: str) -> Path:
        """Return the private directory owned by one explicit session."""
        return self.sessions_dir / self._component(
            session_id, field="session_id"
        )

    def session_metadata_path(self, session_id: str) -> Path:
        """Return the durable metadata file for one explicit session."""
        return self.session_dir(session_id) / "session.json"

    def session_snapshots_path(self, session_id: str) -> Path:
        """Return the durable grounding-snapshot index for one session."""
        return self.session_dir(session_id) / "snapshots.json"

    def session_todos_path(self, session_id: str) -> Path:
        """Return the todo-list file colocated with one session's metadata."""
        return self.session_dir(session_id) / "todos.json"

    def session_audit_path(self, session_id: str) -> Path:
        """Return the append-only audit log owned by one explicit session."""
        return self.session_dir(session_id) / "audit.jsonl"

    def session_transaction_path(self, session_id: str) -> Path:
        """Return the shared transaction identity for one session directory."""
        return self.session_dir(session_id) / ".transaction"

    @property
    def audit_dir(self) -> Path:
        """Directory containing the append-only audit log and payload objects."""
        return self.root / "audit_log"

    @property
    def audit_log_path(self) -> Path:
        """Return the append-only audit JSONL file."""
        return self.audit_dir / "audit.jsonl"

    @property
    def audit_payload_dir(self) -> Path:
        """Return the content-addressed audit payload directory."""
        return self.audit_dir / "payloads"

    @property
    def jobs_store_path(self) -> Path:
        """Return the durable tracked-job registry path."""
        return self.root / "jobs.json"

    @property
    def jobs_store_backup_path(self) -> Path:
        """Return the tracked-job recovery copy path."""
        return self.root / "jobs.json.bak"

    @property
    def jobs_lock_path(self) -> Path:
        """Return the tracked-job cross-process lock path."""
        return self.root / "jobs.lock"

    @property
    def jobs_dir(self) -> Path:
        """Return the directory containing job attempt state and logs."""
        return self.root / "jobs"

    @property
    def jobs_deferred_dir(self) -> Path:
        """Return the managed-job deferred update journal directory."""
        return self.jobs_dir / "deferred"

    @property
    def oauth_clients_path(self) -> Path:
        """Return the approved OAuth client registry path."""
        return self.root / "oauth-clients.json"

    @property
    def control_dir(self) -> Path:
        """Return the directory containing final control-owned durable state."""
        return self.root / "control"

    @property
    def control_executors_path(self) -> Path:
        """Return the durable executor trust registry path."""
        return self.control_dir / "executors.json"

    @property
    def control_sessions_path(self) -> Path:
        """Return the durable control session registry path."""
        return self.control_dir / "sessions.json"

    @property
    def oauth_signing_secret_path(self) -> Path:
        """Return the persisted OAuth JWT signing secret path."""
        return self.root / "oauth-jwt-secret"

    @property
    def oauth_signing_lock_path(self) -> Path:
        """Return the OAuth signing-secret lock path."""
        return self.locks_dir / "oauth-jwt-secret.lock"

    @property
    def downloads_store_path(self) -> Path:
        """Return the tokenized download-link registry path."""
        return self.root / "downloads.json"

    @property
    def downloads_store_backup_path(self) -> Path:
        """Return the download-link registry recovery copy path."""
        return self.root / "downloads.json.bak"

    @property
    def downloads_lock_path(self) -> Path:
        """Return the download-link registry lock path."""
        return self.root / "downloads.lock"

    @property
    def download_snapshots_dir(self) -> Path:
        """Return the private immutable file-link snapshot directory."""
        return self.root / "downloads"

    @property
    def remote_workers_path(self) -> Path:
        """Return the registered remote-worker state path."""
        return self.root / "remote-workers.json"

    @property
    def remote_transfers_dir(self) -> Path:
        """Return the private durable HTTP transfer directory."""
        return self.root / "remote_transfers"

    @property
    def agent_auth_dir(self) -> Path:
        """Return the private Agent Bridge credential directory."""
        return self.root / "agent_auth"

    @property
    def ui_dir(self) -> Path:
        """Return the private Human UI state directory."""
        return self.root / "ui"

    @property
    def ui_local_token_path(self) -> Path:
        """Return the trusted loopback Human UI token path."""
        return self.ui_dir / "local-token"

    @property
    def locks_dir(self) -> Path:
        """Return the shared directory for private state lock files."""
        return self.root / "locks"


class FileStateStore:
    """Filesystem implementation of the shared private-state contract."""

    def __init__(self, root_provider: Callable[[], Path]) -> None:
        self._root_provider = root_provider
        self._transaction_guard = threading.Lock()
        self._transaction_locks: dict[str, tuple[threading.RLock, int]] = {}

    @property
    def layout(self) -> StateLayout:
        """Resolve the current state root lazily so test/config changes are honored."""
        return StateLayout(Path(self._root_provider()).resolve())

    def _contained(self, path: Path) -> Path:
        root = self.layout.root
        candidate = path if path.is_absolute() else root / path
        resolved_parent = candidate.parent.resolve()
        try:
            resolved_parent.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"state path escapes configured root: {path}"
            ) from exc
        return resolved_parent / candidate.name

    def _ensure_private_directory(self, path: Path) -> None:
        """Create one contained directory chain with owner-only permissions."""
        root = self.layout.root
        target = self._contained(path / ".state-directory").parent
        relative = target.relative_to(root)
        current = root
        current.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            current.chmod(0o700)
        for part in relative.parts:
            current /= part
            current.mkdir(exist_ok=True, mode=0o700)
            if current.is_symlink() or not current.is_dir():
                raise OSError(
                    f"state path is not a regular directory: {current}"
                )
            with contextlib.suppress(OSError):
                current.chmod(0o700)

    def read_json(
        self, path: Path, *, max_bytes: int | None = None
    ) -> Any | None:
        """Read bounded JSON without following a final symlink."""
        target = self._contained(path)
        if not target.exists():
            return None
        if target.is_symlink() or not target.is_file():
            raise OSError(f"state path is not a regular file: {target}")
        size = target.stat().st_size
        if max_bytes is not None and size > max_bytes:
            raise ValueError(
                f"Refusing to read {size} state bytes; max is {max_bytes}"
            )
        return json.loads(target.read_text(encoding="utf-8"))

    def write_json(self, path: Path, value: Any) -> None:
        """Atomically write deterministic, owner-private UTF-8 JSON."""
        target = self._contained(path)
        self._ensure_private_directory(target.parent)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        atomic_write_private_text(target, encoded + "\n")

    def remove(self, path: Path, *, recursive: bool = False) -> None:
        """Remove one state file or directory without traversing symlinks."""
        target = self._contained(path)
        if not target.exists() and not target.is_symlink():
            return
        if target.is_symlink():
            target.unlink(missing_ok=True)
            return
        if target.is_dir():
            if not recursive:
                target.rmdir()
                return
            shutil.rmtree(target)
            return
        target.unlink(missing_ok=True)

    def iter_directories(self, path: Path) -> Iterable[Path]:
        """Return non-symlink child directories in stable order."""
        target = self._contained(path)
        if not target.exists():
            return ()
        if target.is_symlink() or not target.is_dir():
            raise OSError(f"state path is not a regular directory: {target}")
        return tuple(
            child
            for child in sorted(target.iterdir(), key=lambda item: item.name)
            if child.is_dir() and not child.is_symlink()
        )

    def _lock_path(self, path: Path) -> Path:
        target = self._contained(path)
        digest = hashlib.sha256(str(target).encode("utf-8")).hexdigest()
        return self.layout.locks_dir / f"state-{digest}.lock"

    def _thread_lock(self, path: Path) -> tuple[str, threading.RLock]:
        key = str(path)
        with self._transaction_guard:
            entry = self._transaction_locks.get(key)
            if entry is None:
                lock = threading.RLock()
                self._transaction_locks[key] = (lock, 1)
                return key, lock
            lock, users = entry
            self._transaction_locks[key] = (lock, users + 1)
            return key, lock

    def _release_thread_lock(self, key: str) -> None:
        with self._transaction_guard:
            entry = self._transaction_locks.get(key)
            if entry is None:
                return
            lock, users = entry
            if users <= 1:
                self._transaction_locks.pop(key, None)
            else:
                self._transaction_locks[key] = (lock, users - 1)

    @contextmanager
    def transaction(self, path: Path) -> Generator[None]:
        """Hold a thread/process lock associated with one durable state identity."""
        self._ensure_private_directory(self.layout.locks_dir)
        lock_path = self._lock_path(path)
        key, thread_lock = self._thread_lock(lock_path)
        try:
            with thread_lock, private_file_lock(lock_path):
                yield
        finally:
            self._release_thread_lock(key)


_STATE_STORE: StateStore | None = None


def _default_state_root() -> Path:
    """Resolve the compatibility fallback state root without an import cycle."""
    return (
        __import__(
            "workgate.config.settings",
            fromlist=["get_settings"],
        )
        .get_settings()
        .state_dir
    )


def configure_state_store(store: StateStore | None) -> StateStore | None:
    """Install a process-wide state store and return the previous binding."""
    global _STATE_STORE
    previous = _STATE_STORE
    _STATE_STORE = store
    return previous


def get_state_store() -> StateStore:
    """Return the configured state store, with a compatibility lazy fallback."""
    global _STATE_STORE
    if _STATE_STORE is None:
        _STATE_STORE = FileStateStore(_default_state_root)
    return _STATE_STORE
