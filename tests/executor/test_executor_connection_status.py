"""Native Executor connection diagnostics must remain small and private."""

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.executor.test_executor_service import _paired_manager
from workgate.executor import connection_status
from workgate.executor.cli import _print_service_status
from workgate.executor.connection_status import ExecutorConnectionStatusStore
from workgate.executor.service import (
    ExecutorServiceState,
    ExecutorServiceStatus,
)
from workgate.persistence import FileStateStore
from workgate.platform.host import HostOS


def _store(tmp_path: Path) -> ExecutorConnectionStatusStore:
    return ExecutorConnectionStatusStore(
        FileStateStore(lambda: tmp_path / "state")
    )


def test_connection_status_tracks_transition_without_sensitive_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1760000000.0]
    monkeypatch.setattr(connection_status.time, "time", lambda: clock[0])
    store = _store(tmp_path)

    assert store.describe() == "unknown (no runtime report)"
    store.record("reconnecting")
    assert store.describe() == "reconnecting"
    clock[0] += 3
    store.record("connected")
    assert store.describe().startswith("connected; last contact ")
    clock[0] += 5
    store.record("reconnecting", "TimeoutError")
    text = store.describe()
    assert text.startswith("reconnecting (TimeoutError); last contact ")
    clock[0] += 1
    store.record("action-required", "executor_revoked")
    assert store.describe().startswith("action-required (executor_revoked);")
    raw = store.path.read_text(encoding="utf-8")
    assert "credential" not in raw
    assert "bearer" not in raw
    assert json.loads(raw)["last_success_at"] == 1760000003.0
    if os.name != "nt":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    clock[0] += 91
    assert store.describe() == (
        "stale (last report: action-required: executor_revoked)"
    )


def test_connection_status_does_not_trust_missing_corrupt_or_future_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1760000000.0]
    monkeypatch.setattr(connection_status.time, "time", lambda: clock[0])
    store = _store(tmp_path)
    store.record("connected")
    store.path.write_text("{bad json", encoding="utf-8")
    assert store.describe() == "unknown (runtime report unavailable)"
    store.record("reconnecting", "TimeoutError")
    assert store.describe() == "reconnecting (TimeoutError)"
    store.path.write_text(
        json.dumps({"phase": "connected", "updated_at": clock[0] + 500}),
        encoding="utf-8",
    )
    assert store.describe() == "stale (no recent runtime report)"
    store.path.write_text(
        json.dumps({"phase": "unknown", "updated_at": clock[0]}),
        encoding="utf-8",
    )
    assert store.describe() == "stale (no recent runtime report)"


def test_windows_status_retries_transient_file_sharing_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.record("reconnecting", "TimeoutError")
    monkeypatch.setattr(connection_status, "host_os", lambda: HostOS.WINDOWS)
    delays: list[float] = []
    monkeypatch.setattr(connection_status.time, "sleep", delays.append)

    read_json = store.state_store.read_json
    write_json = store.state_store.write_json
    calls = {"read": 0, "write": 0}

    def flaky_read(path: Path, *, max_bytes: int | None = None):
        calls["read"] += 1
        if calls["read"] == 1:
            raise PermissionError("simulated transient Windows read lock")
        return read_json(path, max_bytes=max_bytes)

    def flaky_write(path: Path, value: object) -> None:
        calls["write"] += 1
        if calls["write"] == 1:
            raise PermissionError("simulated transient Windows replace lock")
        write_json(path, value)

    monkeypatch.setattr(store.state_store, "read_json", flaky_read)
    monkeypatch.setattr(store.state_store, "write_json", flaky_write)
    store.record("connected")
    assert calls == {"read": 3, "write": 2}
    assert delays == [0.02, 0.02]
    assert store.describe().startswith("connected; last contact ")


@pytest.mark.parametrize(
    ("system", "failures"),
    [
        (HostOS.LINUX, 1),
        (HostOS.WINDOWS, 4),
    ],
)
def test_connection_status_rejects_permanent_write_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system: HostOS,
    failures: int,
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(connection_status, "host_os", lambda: system)
    delays: list[float] = []
    monkeypatch.setattr(connection_status.time, "sleep", delays.append)
    attempts = 0

    def deny_write(path: Path, value: object) -> None:
        nonlocal attempts
        attempts += 1
        raise PermissionError("simulated permanent access denial")

    monkeypatch.setattr(store.state_store, "write_json", deny_write)
    with pytest.raises(PermissionError, match="permanent"):
        store.record("connected")
    assert attempts == failures
    assert delays == [0.02] * (failures - 1)
    assert not store.path.exists()


def test_service_status_reports_connection_separately_from_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager, _profile = _paired_manager(tmp_path, system="Windows")
    monkeypatch.setattr(
        manager,
        "_windows_task",
        lambda: SimpleNamespace(
            status=lambda: {"state": "Running", "last_result": 0}
        ),
    )
    status = manager.status()
    assert status.state is ExecutorServiceState.RUNNING
    assert status.connection_detail == "unknown (no runtime report)"
    _print_service_status(status)
    assert "State: running" in capsys.readouterr().out

    store = ExecutorConnectionStatusStore(manager.state_store)
    store.record("connected")
    status = manager.status()
    assert status.connection_detail
    assert status.connection_detail.startswith("connected; last contact ")
    _print_service_status(status)
    output = capsys.readouterr().out
    assert "State: running" in output
    assert "Connection: connected" in output


def test_stopped_service_does_not_claim_connected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = ExecutorServiceStatus(
        backend="scheduled-task",
        state=ExecutorServiceState.STOPPED,
        installed=True,
        running=False,
        connection_detail="connected",
    )
    _print_service_status(status)
    assert "Connection:" not in capsys.readouterr().out
