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

    def macos(_self, version: str) -> None:
        assert version == TARGET
        events.append("macos")

    monkeypatch.setattr(
        ExecutorServiceManager, "schedule_linux_source_cutover", linux
    )
    monkeypatch.setattr(
        ExecutorServiceManager, "schedule_windows_source_cutover", windows
    )
    monkeypatch.setattr(
        ExecutorServiceManager, "schedule_macos_source_cutover", macos
    )
    return runtime, events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "event"),
    [
        (HostOS.LINUX, "linux"),
        (HostOS.WINDOWS, "windows"),
        (HostOS.MACOS, "macos"),
    ],
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
    assert state.update_status is ExecutorRuntimeUpdateStatus.DISPATCHED
    assert state.update_target_version == TARGET
    assert "native cutover requested" in (state.update_detail or "")


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
    # Direct primitive records a conservative attempted handoff; the policy
    # caller marks a definitive failed schedule as FAILED.
    assert state.update_status is ExecutorRuntimeUpdateStatus.DISPATCHED
    assert "native cutover requested" in (state.update_detail or "")


@pytest.mark.asyncio
async def test_unsupported_host_does_not_schedule(
    managed, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, events = managed
    monkeypatch.setattr(host_module, "host_os", lambda: None)
    with pytest.raises(RuntimeError, match="unsupported"):
        await runtime.handoff_prepared_source_update(TARGET)
    assert events == ["idle"]


@pytest.mark.asyncio
async def test_in_flight_protocol_command_defers_handoff(
    managed, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from workgate.protocol.executor import ExecutorCommand
    from workgate.protocol.ids import new_command_id

    runtime, events = managed
    started = asyncio.Event()
    release = asyncio.Event()

    async def executing(_command):
        started.set()
        await release.wait()
        return {"done": True}

    monkeypatch.setattr(
        runtime, "_execute_protocol_command_with_state", executing
    )
    command = ExecutorCommand(
        id=new_command_id(), op="dashboard_snapshot", args={}
    )
    running = asyncio.create_task(runtime._execute_protocol_command(command))
    await started.wait()
    try:
        with pytest.raises(RuntimeError, match="protocol commands active"):
            await runtime.handoff_prepared_source_update(TARGET)
        assert events == []
    finally:
        release.set()
        await running
    assert runtime._active_protocol_commands == 0
    monkeypatch.setattr(host_module, "host_os", lambda: HostOS.LINUX)
    await runtime.handoff_prepared_source_update(TARGET)
    assert events == ["idle", "linux"]


@pytest.mark.asyncio
async def test_protocol_admission_closed_during_snapshot_and_after_handoff(
    managed, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from workgate.protocol.executor import ExecutorCommand
    from workgate.protocol.ids import new_command_id

    runtime, events = managed
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def idle() -> None:
        entered.set()
        await release.wait()
        events.append("idle")

    async def forbidden(_command) -> None:
        calls.append("executed")

    monkeypatch.setattr(runtime, "require_source_cutover_idle", idle)
    monkeypatch.setattr(
        runtime, "_execute_protocol_command_with_state", forbidden
    )
    monkeypatch.setattr(host_module, "host_os", lambda: HostOS.WINDOWS)
    command = ExecutorCommand(
        id=new_command_id(), op="dashboard_snapshot", args={}
    )
    pending = asyncio.create_task(
        runtime.handoff_prepared_source_update(TARGET)
    )
    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match="cutover in progress"):
            await runtime._execute_protocol_command(command)
        with pytest.raises(RuntimeError, match="protocol commands active"):
            await runtime.handoff_prepared_source_update(TARGET)
    finally:
        release.set()
        await pending
    assert events == ["idle", "windows"]
    with pytest.raises(RuntimeError, match="cutover in progress"):
        await runtime._execute_protocol_command(command)
    assert calls == []


@pytest.mark.asyncio
async def test_failed_dispatch_reopens_protocol_admission(
    managed, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workgate.protocol.executor import ExecutorCommand
    from workgate.protocol.ids import new_command_id

    runtime, events = managed
    monkeypatch.setattr(host_module, "host_os", lambda: HostOS.LINUX)

    def failed(_self, _version: str) -> None:
        raise RuntimeError("native handoff refused")

    monkeypatch.setattr(
        ExecutorServiceManager, "schedule_linux_source_cutover", failed
    )

    async def executing(_command):
        events.append("command")
        return {"done": True}

    monkeypatch.setattr(
        runtime, "_execute_protocol_command_with_state", executing
    )
    command = ExecutorCommand(
        id=new_command_id(), op="dashboard_snapshot", args={}
    )
    with pytest.raises(RuntimeError, match="native handoff refused"):
        await runtime.handoff_prepared_source_update(TARGET)
    assert not runtime._source_cutover_fenced
    await runtime._execute_protocol_command(command)
    assert events == ["idle", "command"]


@pytest.mark.asyncio
async def test_cancelled_snapshot_reopens_protocol_admission(
    managed, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    runtime, _events = managed
    entered = asyncio.Event()

    async def stalled() -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "require_source_cutover_idle", stalled)
    running = asyncio.create_task(
        runtime.handoff_prepared_source_update(TARGET)
    )
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert not runtime._source_cutover_fenced


@pytest.mark.asyncio
async def test_cancelled_native_submission_remains_fenced(
    managed, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    runtime, _events = managed
    monkeypatch.setattr(host_module, "host_os", lambda: HostOS.LINUX)
    submitted = threading.Event()
    finish = threading.Event()

    def delayed(_self, _version: str) -> None:
        submitted.set()
        finish.wait(timeout=5)

    monkeypatch.setattr(
        ExecutorServiceManager, "schedule_linux_source_cutover", delayed
    )
    attempt = asyncio.create_task(
        runtime.handoff_prepared_source_update(TARGET)
    )
    try:
        await asyncio.wait_for(asyncio.to_thread(submitted.wait, 3), timeout=4)
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt
        # The scheduler thread may still register the independent native
        # task after this coroutine was cancelled: no new commands admitted.
        assert runtime._source_cutover_fenced
        with pytest.raises(
            RuntimeError, match="no matching prepared managed source"
        ):
            await runtime.handoff_prepared_source_update(TARGET)
    finally:
        finish.set()
