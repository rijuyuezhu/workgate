"""Durable acceptance record for a standalone managed Executor cutover."""

import contextlib
import hashlib
import json
import math
import os
import subprocess
import tempfile
from collections.abc import Callable, Generator
from pathlib import Path

_TRIAL_FILE = ".executor-cutover.json"
_LOCK_FILE = ".executor-cutover.lock"
_MAX_RECORD_BYTES = 32768


@contextlib.contextmanager
def executor_switch_lock(root: Path) -> Generator[None]:
    """Serialize switches, including explicit recovery after a process exits."""
    path = root / _LOCK_FILE
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError("unexpected Executor cutover lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        if not handle.read(1):
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # pyright: ignore[reportAttributeAccessIssue]
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("another Executor cutover is active") from exc
        yield
        # Closing the handle releases the lock, including on exception or exit.


def _digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("cutover configuration or identity is unavailable")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def begin_trial(
    root: Path,
    previous: Path,
    candidate: Path,
    config: Path,
    profile: Path,
    started_at: float,
) -> None:
    """Persist only identifiers and digests before changing the native service."""
    path = root / _TRIAL_FILE
    if path.exists() or path.is_symlink():
        raise RuntimeError(
            "an interrupted Executor cutover must be recovered first"
        )
    record = {
        "previous": str(previous),
        "candidate": str(candidate),
        "config": str(config),
        "config_digest": _digest(config),
        "profile": str(profile),
        "profile_digest": _digest(profile),
        "started_at": started_at,
    }
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=root,
        prefix=".executor-trial-",
        delete=False,
    ) as output:
        staged = Path(output.name)
        try:
            json.dump(record, output)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            output.close()
            staged.unlink(missing_ok=True)
            raise
    try:
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def finish_trial(root: Path) -> None:
    (root / _TRIAL_FILE).unlink(missing_ok=True)


def recover_trial(
    root: Path,
    current: Path,
    candidate: Path,
    config: Path,
    *,
    binding: Callable[[Path], Path],
    wait_for_poll: Callable[[Path, Path, float], None],
    publish: Callable[[Path, Path], Path],
) -> bool:
    """Reconcile a confirmed old or candidate binding without restarting work.

    The caller has verified both the stable launcher and the requested release,
    and holds the switch lock. Uncertain native ownership remains unresolved.
    """
    path = root / _TRIAL_FILE
    if not path.exists() and not path.is_symlink():
        return False
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("invalid trial file")
        with path.open("rb") as file:
            raw = file.read(_MAX_RECORD_BYTES + 1)
        if len(raw) > _MAX_RECORD_BYTES:
            raise ValueError("trial file too large")
        record = json.loads(raw)
        if not isinstance(record, dict):
            raise ValueError("invalid trial record")
        started = record.get("started_at")
        if (
            not isinstance(started, (float, int))
            or isinstance(started, bool)
            or not math.isfinite(started)
            or started <= 0
        ):
            raise ValueError("invalid trial timestamp")
    except (OSError, ValueError, UnicodeError) as exc:
        raise RuntimeError(
            "invalid interrupted Executor cutover record"
        ) from exc
    if (
        record.get("candidate") != str(candidate)
        or str(current) not in (record.get("previous"), str(candidate))
        or record.get("config") != str(config)
        or record.get("config_digest") != _digest(config)
    ):
        raise RuntimeError(
            "interrupted Executor cutover requires the original target and unchanged configuration"
        )

    def check_profile(profile: Path) -> None:
        if record.get("profile") != str(profile) or record.get(
            "profile_digest"
        ) != _digest(profile):
            raise RuntimeError(
                "Executor pairing identity changed; interrupted recovery unsafe"
            )

    try:
        profile = binding(candidate)
    except (
        RuntimeError,
        OSError,
        subprocess.SubprocessError,
    ) as candidate_error:
        if current == candidate:
            raise RuntimeError(
                "published candidate binding is unconfirmed; inspect native service"
            ) from candidate_error
        try:
            profile = binding(current)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                "interrupted Executor binding is unconfirmed; inspect native service"
            ) from exc
        check_profile(profile)
        finish_trial(root)
        raise RuntimeError(
            "previous Executor is still bound; interrupted trial cleared, rerun switch to retry"
        ) from candidate_error

    check_profile(profile)
    wait_for_poll(candidate, profile, started)
    check_profile(binding(candidate))
    if record.get("config_digest") != _digest(config):
        raise RuntimeError(
            "Executor configuration changed during interrupted recovery"
        )
    if current != candidate:
        publish(root, candidate)
    finish_trial(root)
    return True
