"""Managed source auto-cutover policy: stage, defer, handoff, and reconverge."""

from pathlib import Path

import pytest

from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor.connection import RuntimePolicyAction
from workgate.executor.runtime import (
    SourceCutoverDeferred,
    build_executor_runtime,
)
from workgate.executor.runtime_update import ExecutorRuntimeUpdateStatus
from workgate.executor.service import ExecutorServiceManager
from workgate.platform import host as host_module
from workgate.platform.host import HostOS
from workgate.protocol.executor import (
    ExecutorHelloResponse,
    ExecutorRuntimeOwnership,
)

TARGET = "5.0.0a99"
NEWER = "5.0.0a100"


def _policy(
    target: str = TARGET, *, required: bool = True
) -> ExecutorHelloResponse:
    return ExecutorHelloResponse(
        heartbeat_interval_s=10,
        offline_after_s=30,
        poll_timeout_s=1,
        required_workgate_version=target,
        runtime_update_required=required,
    )


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        default_workdir=tmp_path / "work", state_dir=tmp_path / "state"
    )
    config = resolve_executor_config(settings)
    runtime = build_executor_runtime(
        config,
        enable_control_connection=False,
        managed_service=True,
        runtime_ownership=ExecutorRuntimeOwnership.SOURCE,
    )
    root = tmp_path / "managed" / "versions" / ("a" * 40)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ExecutorServiceManager, "verified_source_checkout", lambda self: root
    )

    async def prepare(self, version: str) -> Path:
        calls.append(("stage", version))
        return root.parent / ("b" * 40)

    def linux(self, version: str) -> None:
        calls.append(("handoff", version))

    monkeypatch.setattr(
        ExecutorServiceManager, "prepare_source_update", prepare
    )
    monkeypatch.setattr(
        ExecutorServiceManager, "schedule_linux_source_cutover", linux
    )
    monkeypatch.setattr(host_module, "host_os", lambda: HostOS.LINUX)

    async def idle() -> None:
        calls.append(("idle", TARGET))

    monkeypatch.setattr(runtime, "require_source_cutover_idle", idle)
    return runtime, calls, config


@pytest.mark.asyncio
@pytest.mark.parametrize("host", [HostOS.LINUX, HostOS.WINDOWS])
async def test_managed_source_stages_then_dispatches_once_and_new_hello_clears(
    source, monkeypatch: pytest.MonkeyPatch, host: HostOS
) -> None:
    runtime, calls, config = source
    monkeypatch.setattr(host_module, "host_os", lambda: host)
    monkeypatch.setattr(
        ExecutorServiceManager,
        "schedule_windows_source_cutover",
        lambda _self, version: calls.append(("handoff", version)),
    )
    assert (
        await runtime._handle_runtime_policy(_policy())
        is RuntimePolicyAction.HANDOFF
    )
    assert calls == [("stage", TARGET), ("idle", TARGET), ("handoff", TARGET)]
    state = runtime.runtime_state_store.load()
    assert state is not None
    assert state.update_status is ExecutorRuntimeUpdateStatus.DISPATCHED
    assert state.update_target_version == TARGET
    assert "awaiting compatible hello" in (state.update_detail or "")
    assert runtime._source_cutover_fenced

    # A separate restarted old Executor must NOT repeat the service switch
    # after a failed external cutover/rollback.
    restarted = build_executor_runtime(
        config,
        enable_control_connection=False,
        managed_service=True,
        runtime_ownership=ExecutorRuntimeOwnership.SOURCE,
    )
    monkeypatch.setattr(
        restarted,
        "handoff_prepared_source_update",
        lambda _target: pytest.fail("old runtime must not dispatch again"),
    )
    assert (
        await restarted._handle_runtime_policy(_policy())
        is RuntimePolicyAction.BLOCK
    )
    assert calls == [("stage", TARGET), ("idle", TARGET), ("handoff", TARGET)]
    unresolved = restarted.runtime_state_store.load()
    assert unresolved is not None
    assert unresolved.update_status is ExecutorRuntimeUpdateStatus.FAILED
    assert "previous version reconnected" in (unresolved.update_detail or "")

    # New native service with exact compatible version completes Hello and
    # becomes IDLE; a queued dispatch is not treated as completion before it.
    assert (
        await restarted._handle_runtime_policy(_policy(required=False))
        is RuntimePolicyAction.CONTINUE
    )
    healthy = restarted.runtime_state_store.load()
    assert healthy is not None
    assert healthy.update_status is ExecutorRuntimeUpdateStatus.IDLE
    assert healthy.update_target_version is None


@pytest.mark.asyncio
async def test_busy_source_defers_and_retries_without_restaging(
    source, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, calls, _config = source
    busy = True

    async def require_idle() -> None:
        calls.append(("idle", TARGET))
        if busy:
            raise SourceCutoverDeferred("user shell still in use")

    monkeypatch.setattr(runtime, "require_source_cutover_idle", require_idle)
    with pytest.raises(ConnectionError, match="user work is active"):
        await runtime._handle_runtime_policy(_policy())
    assert calls == [("stage", TARGET), ("idle", TARGET)]
    assert not runtime._source_cutover_fenced
    state = runtime.runtime_state_store.load()
    assert state is not None
    assert state.update_status is ExecutorRuntimeUpdateStatus.PENDING

    busy = False
    assert (
        await runtime._handle_runtime_policy(_policy())
        is RuntimePolicyAction.HANDOFF
    )
    assert calls == [
        ("stage", TARGET),
        ("idle", TARGET),
        ("idle", TARGET),
        ("handoff", TARGET),
    ]


@pytest.mark.asyncio
async def test_failed_native_schedule_records_safe_failure_not_success(
    source, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, calls, _config = source

    def failed(_self, version: str) -> None:
        calls.append(("handoff", version))
        raise RuntimeError("secret=authentication-token")

    monkeypatch.setattr(
        ExecutorServiceManager, "schedule_linux_source_cutover", failed
    )
    assert (
        await runtime._handle_runtime_policy(_policy())
        is RuntimePolicyAction.BLOCK
    )
    assert calls == [("stage", TARGET), ("idle", TARGET), ("handoff", TARGET)]
    state = runtime.runtime_state_store.load()
    assert state is not None
    assert state.update_status is ExecutorRuntimeUpdateStatus.FAILED
    assert state.update_target_version == TARGET
    assert "RuntimeError" in (state.update_detail or "")
    assert "authentication-token" not in (state.update_detail or "")
    assert not runtime._source_cutover_fenced


@pytest.mark.asyncio
async def test_unconfirmed_old_cutover_blocks_even_a_new_control_target(
    source, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, calls, _config = source
    await runtime._handle_runtime_policy(_policy())
    assert runtime._source_cutover_fenced

    # Control advanced while an earlier externally scheduled native switch
    # remains unconfirmed. Do not schedule a second competing cutover.
    restarted = build_executor_runtime(
        runtime.config,
        enable_control_connection=False,
        managed_service=True,
        runtime_ownership=ExecutorRuntimeOwnership.SOURCE,
    )

    async def idle() -> None:
        return None

    monkeypatch.setattr(restarted, "require_source_cutover_idle", idle)
    assert (
        await restarted._handle_runtime_policy(_policy(NEWER))
        is RuntimePolicyAction.BLOCK
    )
    assert ("stage", NEWER) not in calls
    assert ("handoff", NEWER) not in calls
    final = restarted.runtime_state_store.load()
    assert final is not None
    assert final.update_status is ExecutorRuntimeUpdateStatus.FAILED
    assert final.update_target_version == TARGET


@pytest.mark.asyncio
async def test_control_advances_while_busy_then_installs_latest_only(
    source, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, calls, _config = source
    waiting = True

    async def idle() -> None:
        if waiting:
            raise SourceCutoverDeferred("persistent user shell still running")

    monkeypatch.setattr(runtime, "require_source_cutover_idle", idle)
    with pytest.raises(ConnectionError, match="user work is active"):
        await runtime._handle_runtime_policy(_policy(TARGET))
    assert calls == [("stage", TARGET)]

    # After an intervening upgrade of Control, the old pending stage cannot
    # determine the target. Always stage the newest required version.
    waiting = False
    assert (
        await runtime._handle_runtime_policy(_policy(NEWER))
        is RuntimePolicyAction.HANDOFF
    )
    assert calls == [("stage", TARGET), ("stage", NEWER), ("handoff", NEWER)]
    state = runtime.runtime_state_store.load()
    assert state is not None
    assert state.update_target_version == NEWER
    assert state.update_status is ExecutorRuntimeUpdateStatus.DISPATCHED


@pytest.mark.asyncio
async def test_newly_switched_runtime_catches_a_later_control_version(
    source, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, calls, _config = source
    assert (
        await runtime._handle_runtime_policy(_policy(TARGET))
        is RuntimePolicyAction.HANDOFF
    )
    assert ("handoff", TARGET) in calls

    # The newly installed interpreter has reached the earlier dispatched
    # target, while Control has advanced again before its first Hello.
    import workgate.version as version_module

    monkeypatch.setattr(version_module, "__version__", TARGET)
    restarted = build_executor_runtime(
        runtime.config,
        enable_control_connection=False,
        managed_service=True,
        runtime_ownership=ExecutorRuntimeOwnership.SOURCE,
    )

    async def idle() -> None:
        return None

    monkeypatch.setattr(restarted, "require_source_cutover_idle", idle)
    assert (
        await restarted._handle_runtime_policy(_policy(NEWER))
        is RuntimePolicyAction.HANDOFF
    )
    assert ("stage", NEWER) in calls
    assert ("handoff", NEWER) in calls
    state = restarted.runtime_state_store.load()
    assert state is not None
    assert state.update_status is ExecutorRuntimeUpdateStatus.DISPATCHED
    assert state.update_target_version == NEWER
