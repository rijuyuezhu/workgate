"""Durable, bounded result handoff between Executor execution and Control ACK."""

import hashlib
import json
import threading
import time
from pathlib import Path

from ..app_paths import ensure_private_directory
from ..protocol.executor import ExecutorResult
from ..utils.private_files import atomic_write_private_bytes
from .profile import ExecutorProfile

_MAX_RESULTS = 1024
_MAX_BYTES = 256 * 1024 * 1024


class ResultOutboxFullError(RuntimeError):
    """The bounded outbox cannot accept another result yet."""


class ExecutorResultOutbox:
    """Private result journal scoped to a single paired Control and credential."""

    def __init__(self, state_dir: Path, profile: ExecutorProfile) -> None:
        identity = "\0".join(
            (profile.control_url, profile.executor_id, str(profile.credential))
        )
        scope = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        private_root = ensure_private_directory(state_dir / "executor")
        outbox_root = ensure_private_directory(private_root / "result-outbox")
        self._directory = ensure_private_directory(outbox_root / scope)
        self._lock = threading.Lock()

    def _files(self) -> list[Path]:
        return sorted(self._directory.glob("*.json"))

    def enqueue(self, result: ExecutorResult) -> Path:
        data = json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(data) > _MAX_BYTES:
            raise ResultOutboxFullError(
                "one executor result exceeds outbox byte budget"
            )
        with self._lock:
            files = self._files()
            if (
                len(files) >= _MAX_RESULTS
                or sum(p.stat().st_size for p in files) + len(data) > _MAX_BYTES
            ):
                raise ResultOutboxFullError("executor result outbox is full")
            # The command ID is protocol-validated and the prefix preserves FIFO
            # order after a process restart. Atomic replacement protects crash replay.
            path = self._directory / f"{time.time_ns():020d}-{result.id}.json"
            atomic_write_private_bytes(path, data)
            return path

    def oldest(self) -> Path | None:
        with self._lock:
            files = self._files()
            return files[0] if files else None

    def read(self, path: Path) -> ExecutorResult:
        return ExecutorResult.model_validate_json(path.read_bytes())

    def quarantine(self, path: Path) -> None:
        """Leave a corrupt result available for inspection without blocking replay."""
        with self._lock:
            if path.exists():
                path.replace(path.with_suffix(".invalid"))

    def remove(self, path: Path) -> None:
        with self._lock:
            path.unlink(missing_ok=True)

    def count(self) -> int:
        with self._lock:
            return len(self._files())
