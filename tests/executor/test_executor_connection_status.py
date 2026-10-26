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
