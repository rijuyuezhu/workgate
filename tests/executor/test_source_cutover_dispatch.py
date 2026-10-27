"""A pending source release may be handed off only after an idle check."""

from pathlib import Path

import pytest

from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor.runtime import build_executor_runtime
from workgate.executor.runtime_update import (
    ExecutorRuntimeUpdateStatus,
)
from workgate.executor.service import ExecutorServiceManager
from workgate.platform import host as host_module
from workgate.platform.host import HostOS
from workgate.protocol.executor import ExecutorRuntimeOwnership

TARGET = "5.0.0a99"


@pytest.fixture
def managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(default_workdir=workdir, state_dir=tmp_path / "state")
        ),
        enable_control_connection=False,
        managed_service=True,
        runtime_ownership=ExecutorRuntimeOwnership.SOURCE,
    )
    runtime.runtime_state_store.record_install(
        ownership=ExecutorRuntimeOwnership.SOURCE
    )
    runtime.runtime_state_store.set_update(
        ExecutorRuntimeUpdateStatus.PENDING,
        target_version=TARGET,
        detail="source release prepared; native cutover pending",
    )
    events: list[str] = []

    async def require_idle() -> None:
        events.append("idle")

    monkeypatch.setattr(runtime, "require_source_cutover_idle", require_idle)

    def linux(_self, version: str) -> None:
        assert version == TARGET
        events.append("linux")

    def windows(_self, version: str) -> None:
        assert version == TARGET
        events.append("windows")

    monkeypatch.setattr(
        ExecutorServiceManager, "schedule_linux_source_cutover", linux
    )
    monkeypatch.setattr(
        ExecutorServiceManager, "schedule_windows_source_cutover", windows
    )
    return runtime, events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "event"),
    [(HostOS.LINUX, "linux"), (HostOS.WINDOWS, "windows")],
)
async def test_dispatch_pending_version_through_existing_native_handoff(
    managed, monkeypatch: pytest.MonkeyPatch, host: HostOS, event: str
) -> None:
    runtime, events = managed
    monkeypatch.setattr(host_module, "host_os", lambda: host)
    await runtime.handoff_prepared_source_update(TARGET)
    assert events == ["idle", event]
    state = runtime.runtime_state_store.load()
    assert state is not None
    assert state.update_status is ExecutorRuntimeUpdateStatus.PENDING
    assert state.update_target_version == TARGET
    assert "cutover pending" in (state.update_detail or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "condition", ["wrong-target", "not-ready", "external", "not-managed"]
)
async def test_invalid_source_never_checks_or_schedules(
    managed, condition: str
) -> None:
    runtime, events = managed
    target = TARGET
    if condition == "wrong-target":
        target = "5.0.0a98"
    elif condition == "not-ready":
        runtime.runtime_state_store.set_update(
            ExecutorRuntimeUpdateStatus.FAILED, target_version=TARGET
        )
    elif condition == "external":
        runtime.runtime_state_store.record_install(
            ownership=ExecutorRuntimeOwnership.EXTERNAL_PACKAGE
        )
    else:
        runtime.managed_service = False
    with pytest.raises(RuntimeError, match="no matching prepared"):
        await runtime.handoff_prepared_source_update(target)
    assert events == []


@pytest.mark.asyncio
async def test_busy_resources_cannot_schedule(
    managed, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, events = managed

    async def busy() -> None:
        events.append("idle")
        raise RuntimeError("user shell is active")

    monkeypatch.setattr(runtime, "require_source_cutover_idle", busy)
    with pytest.raises(RuntimeError, match="shell"):
        await runtime.handoff_prepared_source_update(TARGET)
    assert events == ["idle"]
    state = runtime.runtime_state_store.load()
    assert state is not None
    assert state.update_status is ExecutorRuntimeUpdateStatus.PENDING
    assert "cutover pending" in (state.update_detail or "")


@pytest.mark.asyncio
async def test_failed_native_scheduling_does_not_report_dispatch(
    managed, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, events = managed
    monkeypatch.setattr(host_module, "host_os", lambda: HostOS.WINDOWS)

    def failed(_self, version: str) -> None:
        events.append("windows")
        raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr(
        ExecutorServiceManager, "schedule_windows_source_cutover", failed
    )
    with pytest.raises(RuntimeError, match="scheduler unavailable"):
        await runtime.handoff_prepared_source_update(TARGET)
    assert events == ["idle", "windows"]
    state = runtime.runtime_state_store.load()
    assert state is not None
    assert state.update_status is ExecutorRuntimeUpdateStatus.PENDING
    assert "cutover pending" in (state.update_detail or "")


@pytest.mark.asyncio
async def test_unsupported_host_does_not_schedule(
    managed, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, events = managed
    monkeypatch.setattr(host_module, "host_os", lambda: HostOS.MACOS)
    with pytest.raises(RuntimeError, match="unsupported"):
        await runtime.handoff_prepared_source_update(TARGET)
    assert events == ["idle"]
