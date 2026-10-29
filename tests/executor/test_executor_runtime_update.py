from pathlib import Path

import httpx
import pytest

from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor import runtime_update
from workgate.executor.connection import RuntimePolicyAction
from workgate.executor.runtime import build_executor_runtime
from workgate.executor.runtime_update import (
    ExecutorRuntimeStateStore,
    ExecutorRuntimeUpdateStatus,
)
from workgate.persistence import FileStateStore
from workgate.protocol.executor import (
    ExecutorHelloResponse,
    ExecutorRuntimeOwnership,
)


def _store(tmp_path: Path) -> ExecutorRuntimeStateStore:
    return ExecutorRuntimeStateStore(FileStateStore(lambda: tmp_path / "state"))


def _freeze_runtime(monkeypatch: pytest.MonkeyPatch, executable: Path) -> None:
    monkeypatch.setattr(runtime_update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime_update.sys, "executable", str(executable))


def test_control_managed_authority_is_bound_to_the_published_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first-workgate"
    second = tmp_path / "second-workgate"
    first.write_text("first")
    second.write_text("second")
    store = _store(tmp_path)

    _freeze_runtime(monkeypatch, first)
    state = store.record_install(
        ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED
    )
    assert state.ownership is ExecutorRuntimeOwnership.CONTROL_MANAGED
    assert state.executable == str(first.absolute())

    _freeze_runtime(monkeypatch, second)
    state = store.service_runtime_state()
    assert state.ownership is ExecutorRuntimeOwnership.SELF_CONTAINED
    assert state.executable is None

    persisted = store.load()
    assert persisted == state


def test_managed_service_provenance_is_persisted_before_hello(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "workgate"
    _freeze_runtime(monkeypatch, executable)
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                default_workdir=tmp_path / "workspace",
                state_dir=tmp_path / "state",
            )
        ),
        enable_control_connection=False,
        managed_service=True,
        runtime_ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED,
    )

    state = runtime._runtime_state()

    assert state.ownership is ExecutorRuntimeOwnership.CONTROL_MANAGED
    assert state.executable == str(executable.absolute())
    assert runtime.runtime_state_store.load() == state


def test_non_frozen_runtime_cannot_claim_control_managed_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(runtime_update.sys, "frozen", raising=False)
    store = _store(tmp_path)

    with pytest.raises(
        RuntimeError, match="requires a self-contained executable"
    ):
        store.record_install(ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED)


@pytest.mark.asyncio
async def test_source_runtime_reports_owner_required_instead_of_self_mutating(
    tmp_path: Path,
) -> None:
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                default_workdir=tmp_path / "workspace",
                state_dir=tmp_path / "state",
            )
        ),
        enable_control_connection=False,
        managed_service=True,
        runtime_ownership=ExecutorRuntimeOwnership.SOURCE,
    )
    policy = ExecutorHelloResponse(
        heartbeat_interval_s=10,
        offline_after_s=30,
        poll_timeout_s=1,
        required_workgate_version="next-version",
        runtime_update_required=True,
    )

    action = await runtime._handle_runtime_policy(policy)

    assert action is RuntimePolicyAction.BLOCK
    state = runtime.runtime_state_store.load()
    assert state is not None
    assert state.ownership is ExecutorRuntimeOwnership.SOURCE
    assert state.update_status is ExecutorRuntimeUpdateStatus.REQUIRED
    assert state.update_target_version == "next-version"


@pytest.mark.asyncio
async def test_external_package_service_refuses_control_mutation(
    tmp_path: Path,
) -> None:
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                default_workdir=tmp_path / "workspace",
                state_dir=tmp_path / "state",
            )
        ),
        enable_control_connection=False,
        managed_service=True,
        runtime_ownership=ExecutorRuntimeOwnership.EXTERNAL_PACKAGE,
    )
    policy = ExecutorHelloResponse(
        heartbeat_interval_s=10,
        offline_after_s=30,
        poll_timeout_s=1,
        required_workgate_version="next-version",
        runtime_update_required=True,
    )

    action = await runtime._handle_runtime_policy(policy)

    assert action is RuntimePolicyAction.BLOCK
    state = runtime.runtime_state_store.load()
    assert state is not None
    assert state.ownership is ExecutorRuntimeOwnership.EXTERNAL_PACKAGE
    assert state.update_status is ExecutorRuntimeUpdateStatus.REQUIRED
    assert state.update_target_version == "next-version"


@pytest.mark.asyncio
async def test_foreground_source_runtime_reports_owner_action(
    tmp_path: Path,
) -> None:
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                default_workdir=tmp_path / "workspace",
                state_dir=tmp_path / "state",
            )
        ),
        enable_control_connection=False,
        managed_service=False,
    )
    policy = ExecutorHelloResponse(
        heartbeat_interval_s=10,
        offline_after_s=30,
        poll_timeout_s=1,
        required_workgate_version="next-version",
        runtime_update_required=True,
    )

    with pytest.raises(RuntimeError, match="installation owner"):
        await runtime._handle_runtime_policy(policy)

    assert runtime.runtime_state_store.load() is None


def test_runtime_state_validation_and_required_update_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="requires a target version"):
        store.set_update(ExecutorRuntimeUpdateStatus.PENDING)
    with pytest.raises(RuntimeError, match="ownership is not recorded"):
        store.set_update(ExecutorRuntimeUpdateStatus.IDLE)

    monkeypatch.setattr(
        runtime_update,
        "detect_runtime_ownership",
        lambda: ExecutorRuntimeOwnership.UNKNOWN,
    )
    state = store.service_runtime_state()
    assert state.ownership is ExecutorRuntimeOwnership.UNKNOWN

    path = store.state_store.layout.executor_runtime_path
    with store.state_store.transaction(path):
        store.state_store.write_json(path, {"version": 999})
    with pytest.raises(RuntimeError, match="Invalid executor runtime state"):
        store.load()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage_fails", [False, True])
async def test_managed_source_policy_stages_and_requests_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage_fails: bool,
) -> None:
    from workgate.executor.service import ExecutorServiceManager

    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                default_workdir=tmp_path / "workspace",
                state_dir=tmp_path / "state",
            )
        ),
        enable_control_connection=False,
        managed_service=True,
        runtime_ownership=ExecutorRuntimeOwnership.SOURCE,
    )
    checkout = tmp_path / "managed" / "versions" / ("a" * 40)
    monkeypatch.setattr(
        ExecutorServiceManager,
        "verified_source_checkout",
        lambda _self: checkout,
    )
    staged = []

    async def prepare(_self, target: str) -> Path:
        staged.append(target)
        if stage_fails:
            raise RuntimeError("secret bearer-token=do-not-persist")
        return tmp_path / "managed" / "versions" / ("b" * 40)

    monkeypatch.setattr(
        ExecutorServiceManager, "prepare_source_update", prepare
    )
    handed_off: list[str] = []

    async def handoff(target: str) -> None:
        handed_off.append(target)

    monkeypatch.setattr(runtime, "handoff_prepared_source_update", handoff)
    policy = ExecutorHelloResponse(
        heartbeat_interval_s=10,
        offline_after_s=30,
        poll_timeout_s=1,
        required_workgate_version="5.0.0a99",
        runtime_update_required=True,
    )

    action = await runtime._handle_runtime_policy(policy)

    assert action is (
        RuntimePolicyAction.BLOCK
        if stage_fails
        else RuntimePolicyAction.HANDOFF
    )
    assert staged == ["5.0.0a99"]
    assert handed_off == ([] if stage_fails else ["5.0.0a99"])
    state = runtime.runtime_state_store.load()
    assert state is not None
    assert state.ownership is ExecutorRuntimeOwnership.SOURCE
    assert state.update_target_version == "5.0.0a99"
    assert state.update_status is (
        ExecutorRuntimeUpdateStatus.FAILED
        if stage_fails
        else ExecutorRuntimeUpdateStatus.PENDING
    )
    assert "bearer-token" not in str(state.update_detail)
    if not stage_fails:
        assert "cutover pending" in str(state.update_detail)


@pytest.mark.asyncio
async def test_unverifiable_source_stays_owner_managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workgate.executor.service import ExecutorServiceManager

    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(default_workdir=tmp_path, state_dir=tmp_path / "state")
        ),
        enable_control_connection=False,
        managed_service=True,
        runtime_ownership=ExecutorRuntimeOwnership.SOURCE,
    )

    def unavailable(_self):
        raise RuntimeError("native source binding unavailable")

    async def forbidden(_self, target: str):
        raise AssertionError("must not prepare unverified source")

    monkeypatch.setattr(
        ExecutorServiceManager, "verified_source_checkout", unavailable
    )
    monkeypatch.setattr(
        ExecutorServiceManager, "prepare_source_update", forbidden
    )
    policy = ExecutorHelloResponse(
        heartbeat_interval_s=10,
        offline_after_s=30,
        poll_timeout_s=1,
        required_workgate_version="5.0.0a99",
        runtime_update_required=True,
    )

    assert (
        await runtime._handle_runtime_policy(policy)
        is RuntimePolicyAction.BLOCK
    )
    state = runtime.runtime_state_store.load()
    assert state is not None
    assert state.update_status is ExecutorRuntimeUpdateStatus.REQUIRED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "retry"),
    [
        (httpx.ConnectError("temporary offline"), True),
        (httpx.ReadTimeout("timeout"), True),
        (ConnectionError("git ls-remote unavailable"), True),
        (TimeoutError("operation expired"), True),
        (
            httpx.HTTPStatusError(
                "server failure",
                request=httpx.Request("GET", "https://github.test/release"),
                response=httpx.Response(503),
            ),
            True,
        ),
        (
            httpx.HTTPStatusError(
                "rate limited",
                request=httpx.Request("GET", "https://github.test/release"),
                response=httpx.Response(429),
            ),
            True,
        ),
        (
            httpx.HTTPStatusError(
                "unauthorized",
                request=httpx.Request("GET", "https://github.test/release"),
                response=httpx.Response(401),
            ),
            False,
        ),
        (
            httpx.HTTPStatusError(
                "release absent",
                request=httpx.Request("GET", "https://github.test/release"),
                response=httpx.Response(404),
            ),
            False,
        ),
        (RuntimeError("release tag and commit disagree"), False),
    ],
)
async def test_source_staging_retries_only_transient_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    retry: bool,
) -> None:
    from workgate.executor.service import ExecutorServiceManager

    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(default_workdir=tmp_path, state_dir=tmp_path / "state")
        ),
        enable_control_connection=False,
        managed_service=True,
        runtime_ownership=ExecutorRuntimeOwnership.SOURCE,
    )
    checkout = tmp_path / "versions" / ("a" * 40)
    monkeypatch.setattr(
        ExecutorServiceManager,
        "verified_source_checkout",
        lambda _self: checkout,
    )
    prepared = []

    async def prepare(_self, version: str) -> Path:
        prepared.append(version)
        raise failure

    monkeypatch.setattr(
        ExecutorServiceManager, "prepare_source_update", prepare
    )
    policy = ExecutorHelloResponse(
        heartbeat_interval_s=10,
        offline_after_s=30,
        poll_timeout_s=1,
        required_workgate_version="5.0.0a99",
        runtime_update_required=True,
    )
    if retry:
        with pytest.raises(
            ConnectionError, match="temporarily unavailable"
        ) as exc_info:
            await runtime._handle_runtime_policy(policy)
        assert exc_info.value.__cause__ is failure
    else:
        assert (
            await runtime._handle_runtime_policy(policy)
            is RuntimePolicyAction.BLOCK
        )
    assert prepared == ["5.0.0a99"]
    state = runtime.runtime_state_store.load()
    assert state is not None
    assert state.update_status is ExecutorRuntimeUpdateStatus.FAILED
    assert state.update_target_version == "5.0.0a99"
    assert "https://github.test" not in (state.update_detail or "")


@pytest.mark.asyncio
async def test_legacy_control_managed_requires_explicit_source_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "old-frozen-workgate"
    original = b"keep the existing frozen runtime for explicit rollback"
    executable.write_bytes(original)
    _freeze_runtime(monkeypatch, executable)
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(default_workdir=tmp_path, state_dir=tmp_path / "state")
        ),
        enable_control_connection=False,
        managed_service=True,
        runtime_ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED,
    )
    policy = ExecutorHelloResponse(
        heartbeat_interval_s=10,
        offline_after_s=30,
        poll_timeout_s=1,
        required_workgate_version="5.0.0a99",
        runtime_update_required=True,
    )
    assert (
        await runtime._handle_runtime_policy(policy)
        is RuntimePolicyAction.BLOCK
    )
    state = runtime.runtime_state_store.load()
    assert state is not None
    assert state.ownership is ExecutorRuntimeOwnership.CONTROL_MANAGED
    assert state.executable == str(executable.absolute())
    assert state.update_status is ExecutorRuntimeUpdateStatus.REQUIRED
    assert state.update_target_version == "5.0.0a99"
    assert "migrate the legacy frozen Executor" in (state.update_detail or "")
    assert executable.read_bytes() == original
