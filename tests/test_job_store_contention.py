import contextlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from workgate.config.settings import clear_settings_cache
from workgate.executor.tool_session.store import get_tool_session_store
from workgate.jobs import managed as jobs_managed
from workgate.jobs import persistence as job_persistence
from workgate.jobs import recovery as jobs_recovery
from workgate.jobs import state as job_state
from workgate.persistence import FileStateStore
from workgate.protocol.ids import new_session_id
from workgate.utils import private_files

pytestmark = pytest.mark.usefixtures("managed_jobs_runtime_owner")


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    store = get_tool_session_store()
    store.clear()
    return store.create_session(
        session_id=str(new_session_id()), workdir="."
    ).session_id


def _seed_managed_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    job_id: str = "job_contended",
) -> tuple[str, Path]:
    session_id = _configure(tmp_path, monkeypatch)
    log_path = job_persistence.attempt_paths(job_id, 1)["log"]
    log_path.write_text("", encoding="utf-8")
    now = time.time()
    job_persistence.save_store(
        {
            "version": job_persistence.JOB_STORE_VERSION,
            "jobs": [
                {
                    "job_id": job_id,
                    "kind": "managed",
                    "managed_kind": "test-contention",
                    "managed_payload": {},
                    "name": "contention",
                    "status": "running",
                    "command": "managed contention",
                    "cwd": str(tmp_path),
                    "session_id": session_id,
                    "shell_id": None,
                    "backend": "managed",
                    "log_path": str(log_path),
                    "created_at": now,
                    "updated_at": now,
                    "last_started_at": now,
                    "completed_at": None,
                    "exit_code": None,
                    "error": None,
                    "log_truncated": False,
                    "output_bytes": 0,
                    "attempts": 1,
                    "progress": None,
                    "result": None,
                }
            ],
        }
    )
    return session_id, log_path


def _stored_job(session_id: str, job_id: str) -> dict[str, object]:
    store = job_persistence.load_store()
    return dict(job_state.find_session_job(store, session_id, job_id))


def test_file_state_store_serializes_shared_transaction_lock_users(
    tmp_path: Path,
) -> None:
    store = FileStateStore(lambda: tmp_path / "state")
    identity = Path("shared.json")
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    failures: list[BaseException] = []

    def run_first() -> None:
        try:
            with store.transaction(identity):
                first_entered.set()
                if not release_first.wait(timeout=2):
                    raise TimeoutError("first transaction was not released")
        except BaseException as exc:
            failures.append(exc)

    def run_second() -> None:
        try:
            if not first_entered.wait(timeout=2):
                raise TimeoutError("first transaction never acquired")
            with store.transaction(identity):
                second_entered.set()
        except BaseException as exc:
            failures.append(exc)

    first = threading.Thread(target=run_first)
    second = threading.Thread(target=run_second)
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with store._transaction_guard:
            users = [entry[1] for entry in store._transaction_locks.values()]
        if users == [2]:
            break
        time.sleep(0.01)
    else:
        pytest.fail("second transaction did not share the thread lock")

    assert not second_entered.is_set()
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()
    assert failures == []
    assert store._transaction_locks == {}


def test_private_file_lock_retries_and_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "private.lock"
    private_files._ensure_lock_file(lock_path)
    attempts = 0

    def eventually_lock(handle):  # noqa: ANN001, ARG001
        nonlocal attempts
        attempts += 1
        return attempts == 3

    monkeypatch.setattr(private_files, "_try_lock_handle", eventually_lock)
    monkeypatch.setattr(private_files.time, "sleep", lambda _seconds: None)
    with lock_path.open("r+b") as handle:
        private_files._lock_handle(handle, timeout_s=1)
    assert attempts == 3

    monkeypatch.setattr(
        private_files, "_try_lock_handle", lambda _handle: False
    )
    with (
        lock_path.open("r+b") as handle,
        pytest.raises(TimeoutError, match="private file lock"),
    ):
        private_files._lock_handle(handle, timeout_s=0)


def test_private_lock_file_preparation_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zero = tmp_path / "zero.lock"
    zero.write_bytes(b"")
    real_open = private_files.os.open

    def fail_repair(path, flags, mode=0o777):  # noqa: ANN001
        if Path(path) == zero and flags & private_files.os.O_EXCL:
            raise FileExistsError
        if Path(path) == zero:
            raise PermissionError
        return real_open(path, flags, mode)

    monkeypatch.setattr(private_files.os, "open", fail_repair)
    with pytest.raises(TimeoutError, match="preparing private lock"):
        private_files._ensure_lock_file(zero, timeout_s=0)

    denied = tmp_path / "denied.lock"

    def fail_create(path, flags, mode=0o777):  # noqa: ANN001, ARG001
        raise PermissionError

    monkeypatch.setattr(private_files.os, "open", fail_create)
    with pytest.raises(TimeoutError, match="preparing private lock"):
        private_files._ensure_lock_file(denied, timeout_s=0)

    monkeypatch.setattr(
        private_files.os,
        "open",
        lambda path, flags, mode=0o777: real_open(path, flags, mode),
    )
    opened = tmp_path / "opened.lock"
    private_files._ensure_lock_file(opened)
    monkeypatch.setattr(
        type(opened),
        "open",
        lambda self, *args, **kwargs: (_ for _ in ()).throw(PermissionError()),
    )
    with pytest.raises(TimeoutError, match="opening private lock"):
        private_files._open_lock_handle(opened, timeout_s=0)


def test_windows_lock_contention_error_is_classified() -> None:
    class WindowsBusyError(OSError):
        winerror = 32

    busy = WindowsBusyError(999, "busy")

    assert private_files._is_lock_contention_error(busy, windows=True)
    assert not private_files._is_lock_contention_error(busy, windows=False)


def test_job_store_thread_lock_timeout_is_bounded_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with jobs_recovery._JOB_STORE_THREAD_LOCK:
            acquired.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert acquired.wait(timeout=1)
    monkeypatch.setattr(jobs_recovery, "JOB_STORE_LOCK_TIMEOUT_S", 0.01)
    try:
        with (
            pytest.raises(TimeoutError, match="job store is busy") as raised,
            jobs_recovery.store_transaction(),
        ):
            raise AssertionError("transaction body must not run")
    finally:
        release.set()
        holder.join(timeout=1)

    assert not holder.is_alive()
    assert str(tmp_path) not in str(raised.value)


def test_job_store_file_lock_timeout_is_bounded_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)

    @contextlib.contextmanager
    def busy_lock(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise TimeoutError("contended")
        yield  # pragma: no cover

    monkeypatch.setattr(jobs_recovery, "private_file_lock", busy_lock)
    with (
        pytest.raises(TimeoutError, match="job store is busy") as raised,
        jobs_recovery.store_transaction(),
    ):
        raise AssertionError("transaction body must not run")

    assert str(tmp_path) not in str(raised.value)


def test_job_store_does_not_translate_business_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)

    with (
        pytest.raises(TimeoutError, match="business timeout"),
        jobs_recovery.store_transaction(),
    ):
        raise TimeoutError("business timeout")


def test_managed_updates_retry_then_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, log_path = _seed_managed_job(tmp_path, monkeypatch)
    original_transaction = jobs_managed._store_transaction
    failures = 0

    @contextlib.contextmanager
    def flaky_transaction():
        nonlocal failures
        if failures:
            failures -= 1
            raise TimeoutError("busy")
        with original_transaction() as store:
            yield store

    monkeypatch.setattr(jobs_managed, "_store_transaction", flaky_transaction)
    monkeypatch.setattr(jobs_managed.time, "sleep", lambda _seconds: None)

    failures = 1
    jobs_managed._append_managed_log(
        session_id, "job_contended", str(log_path), "hello"
    )
    failures = 1
    jobs_managed._update_managed_progress(
        session_id, "job_contended", {"phase": "copying"}
    )
    failures = 1
    jobs_managed._finish_managed_job(
        session_id,
        "job_contended",
        status="succeeded",
        exit_code=0,
        error=None,
        result={"copied": True},
    )

    stored = _stored_job(session_id, "job_contended")
    assert stored["output_bytes"] == len(b"hello\n")
    assert stored["progress"] == {"phase": "copying"}
    assert stored["status"] == "succeeded"
    assert stored["result"] == {"copied": True}
    assert not jobs_recovery.managed_deferred_update_dir(create=False).exists()


def test_managed_updates_defer_in_order_and_replay_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, log_path = _seed_managed_job(tmp_path, monkeypatch)
    original_transaction = jobs_managed._store_transaction

    @contextlib.contextmanager
    def busy_transaction():
        raise TimeoutError("busy")
        yield  # pragma: no cover

    monkeypatch.setattr(jobs_managed, "_store_transaction", busy_transaction)
    monkeypatch.setattr(jobs_managed.time, "sleep", lambda _seconds: None)
    wall_clock = iter([3, 2, 1])
    monkeypatch.setattr(jobs_recovery.time, "time_ns", lambda: next(wall_clock))

    jobs_managed._append_managed_log(
        session_id, "job_contended", str(log_path), "hello"
    )
    jobs_managed._update_managed_progress(
        session_id, "job_contended", {"phase": "copying"}
    )
    jobs_managed._finish_managed_job(
        session_id,
        "job_contended",
        status="succeeded",
        exit_code=0,
        error=None,
        result={"copied": True},
    )

    deferred_dir = jobs_recovery.managed_deferred_update_dir(create=False)
    deferred_paths = sorted(deferred_dir.glob("*.json"))
    assert [
        json.loads(path.read_text(encoding="utf-8"))["operation"]
        for path in deferred_paths
    ] == ["append_log", "update_progress", "finish"]
    if os.name != "nt":
        # Windows does not expose POSIX owner/group/other permission semantics.
        assert all(
            (path.stat().st_mode & 0o077) == 0 for path in deferred_paths
        )

    original_remove = jobs_recovery.remove_managed_deferred_updates
    monkeypatch.setattr(
        jobs_recovery, "remove_managed_deferred_updates", lambda _paths: None
    )
    with original_transaction():
        pass
    with original_transaction():
        pass

    interrupted = _stored_job(session_id, "job_contended")
    assert interrupted["output_bytes"] == len(b"hello\n")
    assert interrupted["status"] == "succeeded"
    applied_ids = interrupted[jobs_recovery.MANAGED_DEFERRED_APPLIED_KEY]
    assert isinstance(applied_ids, list)
    assert len(applied_ids) == 3

    monkeypatch.setattr(
        jobs_recovery, "remove_managed_deferred_updates", original_remove
    )
    with original_transaction():
        pass
    assert not list(deferred_dir.glob("*.json"))
    with original_transaction():
        pass

    stored = _stored_job(session_id, "job_contended")
    assert stored["output_bytes"] == len(b"hello\n")
    assert stored["progress"] == {"phase": "copying"}
    assert stored["status"] == "succeeded"
    assert stored["result"] == {"copied": True}
    assert jobs_recovery.MANAGED_DEFERRED_APPLIED_KEY not in stored


def test_deferred_sequence_continues_existing_journal_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    directory = jobs_recovery.managed_deferred_update_dir()
    (directory / f"{42:020d}-00000000000000000001-old.json").write_text(
        "{}", encoding="utf-8"
    )
    monkeypatch.setattr(jobs_recovery, "_MANAGED_DEFERRED_NEXT_SEQUENCE", None)

    assert jobs_recovery.next_managed_deferred_sequence() == 43
    assert jobs_recovery.next_managed_deferred_sequence() == 44


def test_deferred_records_are_session_bound_and_strictly_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, _log_path = _seed_managed_job(tmp_path, monkeypatch)
    deferred_dir = jobs_recovery.managed_deferred_update_dir()

    wrong_session = jobs_recovery.write_managed_deferred_update(
        "WRONG001",
        "job_contended",
        "append_log",
        {"bytes": 20, "updated_at": time.time()},
    )
    invalid = deferred_dir / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    mismatched = deferred_dir / "mismatched.json"
    mismatched.write_text(
        json.dumps(
            {
                "version": jobs_recovery.MANAGED_DEFERRED_UPDATE_VERSION,
                "update_id": "different",
                "session_id": session_id,
                "job_id": "job_contended",
                "operation": "append_log",
                "payload": {},
            }
        ),
        encoding="utf-8",
    )
    symlink = deferred_dir / "linked.json"
    try:
        symlink.symlink_to(wrong_session.name)
    except OSError:
        symlink = None

    with jobs_recovery.store_transaction():
        pass

    stored = _stored_job(session_id, "job_contended")
    assert stored["output_bytes"] == 0
    assert not wrong_session.exists()
    assert not invalid.exists()
    assert not mismatched.exists()
    if symlink is not None:
        assert not symlink.exists()


def test_unreadable_deferred_record_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_managed_job(tmp_path, monkeypatch)
    record = jobs_recovery.write_managed_deferred_update(
        "WRONG001", "job_contended", "append_log", {"bytes": 1}
    )
    original_reader = jobs_recovery.read_managed_deferred_record

    def fail_one(path: Path):
        if path == record:
            raise PermissionError("unreadable")
        return original_reader(path)

    monkeypatch.setattr(jobs_recovery, "read_managed_deferred_record", fail_one)
    with jobs_recovery.store_transaction():
        pass

    assert record.exists()


def test_missing_managed_log_row_does_not_create_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _configure(tmp_path, monkeypatch)
    log_path = job_persistence.attempt_paths("job_missing", 1)["log"]

    jobs_managed._append_managed_log(
        session_id, "job_missing", str(log_path), "orphaned"
    )

    assert log_path.read_text(encoding="utf-8") == "orphaned\n"
    assert not jobs_recovery.managed_deferred_update_dir(create=False).exists()
    with pytest.raises(KeyError, match="job not found"):
        jobs_managed._update_managed_progress(
            session_id, "job_missing", {"phase": "missing"}
        )


@pytest.mark.asyncio
async def test_managed_stop_reconciles_deferred_cancellation_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _configure(tmp_path, monkeypatch)
    jobs_managed.managed_jobs_runtime().handlers.pop("test-deferred-stop", None)
    started_handler = threading.Event()

    async def handler(context, payload):  # noqa: ANN001, ARG001
        started_handler.set()
        await __import__("asyncio").Event().wait()

    jobs_managed.register_managed_job_handler("test-deferred-stop", handler)
    started = await jobs_managed.start_managed_job(
        session_id,
        "test-deferred-stop",
        {},
        name="deferred-stop",
    )
    for _ in range(100):
        if started_handler.is_set():
            break
        await __import__("asyncio").sleep(0.01)
    assert started_handler.is_set()

    def defer_update(
        operation: str,
        update_session_id: str,
        job_id: str,
        payload: dict[str, object],
    ) -> None:
        jobs_recovery.write_managed_deferred_update(
            update_session_id, job_id, operation, payload
        )

    monkeypatch.setattr(jobs_managed, "_managed_store_update", defer_update)
    stopped = await jobs_managed.stop_managed_job_without_session_admission(
        session_id, started.job_id
    )

    assert stopped.job.status == "stopped"
    assert stopped.job.error is None
    assert "job cancelled" in job_persistence.attempt_paths(started.job_id, 1)[
        "log"
    ].read_text(encoding="utf-8")
    assert not list(
        jobs_recovery.managed_deferred_update_dir(create=False).glob("*.json")
    )
    jobs_managed.managed_jobs_runtime().handlers.pop("test-deferred-stop", None)
