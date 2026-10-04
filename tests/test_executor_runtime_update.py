import hashlib
import io
import tarfile
from pathlib import Path

import httpx
import pytest

from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor import runtime_update
from workgate.executor.connection import RuntimePolicyAction
from workgate.executor.profile import ExecutorProfile
from workgate.executor.runtime import build_executor_runtime
from workgate.executor.runtime_update import (
    ExecutorRuntimeStateStore,
    ExecutorRuntimeUpdateStatus,
    apply_control_managed_update,
)
from workgate.persistence import FileStateStore
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.executor import (
    ExecutorHelloResponse,
    ExecutorRuntimeOwnership,
)
from workgate.protocol.ids import new_executor_id


def _store(tmp_path: Path) -> ExecutorRuntimeStateStore:
    return ExecutorRuntimeStateStore(FileStateStore(lambda: tmp_path / "state"))


def _make_archive(*, target: str, version: str) -> bytes:
    script = (f"#!/bin/sh\nprintf '%s\\n' 'workgate {version}'\n").encode()
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as bundle:
        info = tarfile.TarInfo(f"workgate-{target}/workgate")
        info.size = len(script)
        info.mode = 0o700
        bundle.addfile(info, io.BytesIO(script))
    return payload.getvalue()


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

    with pytest.raises(RuntimeError, match="requires a standalone executable"):
        store.record_install(ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED)


@pytest.mark.asyncio
async def test_control_managed_update_replaces_verified_runtime_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_name = "linux-x86_64"
    target_version = "9.9.9"
    target = tmp_path / "workgate"
    target.write_text("#!/bin/sh\nprintf '%s\\n' 'workgate old'\n")
    target.chmod(0o700)
    _freeze_runtime(monkeypatch, target)

    store = _store(tmp_path)
    store.record_install(ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED)
    archive = _make_archive(target=target_name, version=target_version)
    digest = hashlib.sha256(archive).hexdigest()
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sha256"):
            return httpx.Response(200, text=digest + "\n")
        if request.url.path.endswith("/archive"):
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    def client_factory(**kwargs):
        return real_client(
            transport=httpx.MockTransport(handler),
            follow_redirects=bool(kwargs.get("follow_redirects", False)),
            timeout=kwargs.get("timeout"),
        )

    monkeypatch.setattr(runtime_update, "_target_name", lambda: target_name)
    monkeypatch.setattr(runtime_update.httpx, "AsyncClient", client_factory)

    updated = await apply_control_managed_update(
        control_url="https://control.test",
        target_version=target_version,
        store=store,
    )

    assert updated == target
    assert target.read_text().endswith(f"'workgate {target_version}'\n")
    assert target.stat().st_mode & 0o111
    state = store.load()
    assert state is not None
    assert state.ownership is ExecutorRuntimeOwnership.CONTROL_MANAGED
    assert state.update_status is ExecutorRuntimeUpdateStatus.IDLE
    assert state.update_target_version is None
    assert not list(tmp_path.glob(".workgate.*"))


@pytest.mark.asyncio
async def test_failed_staged_validation_preserves_previous_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_name = "linux-x86_64"
    target_version = "9.9.9"
    target = tmp_path / "workgate"
    original = b"old-runtime"
    target.write_bytes(original)
    target.chmod(0o700)
    _freeze_runtime(monkeypatch, target)

    store = _store(tmp_path)
    store.record_install(ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED)
    archive = _make_archive(target=target_name, version=target_version)
    digest = hashlib.sha256(archive).hexdigest()
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sha256"):
            return httpx.Response(200, text=digest + "\n")
        if request.url.path.endswith("/archive"):
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    def client_factory(**kwargs):
        return real_client(
            transport=httpx.MockTransport(handler),
            follow_redirects=bool(kwargs.get("follow_redirects", False)),
            timeout=kwargs.get("timeout"),
        )

    def fail_validation(_path: Path, _version: str) -> None:
        raise RuntimeError("staged runtime failed validation")

    monkeypatch.setattr(runtime_update, "_target_name", lambda: target_name)
    monkeypatch.setattr(runtime_update, "_validate_executable", fail_validation)
    monkeypatch.setattr(runtime_update.httpx, "AsyncClient", client_factory)

    with pytest.raises(RuntimeError, match="staged runtime failed"):
        await apply_control_managed_update(
            control_url="https://control.test",
            target_version=target_version,
            store=store,
        )

    assert target.read_bytes() == original
    state = store.load()
    assert state is not None
    assert state.update_status is ExecutorRuntimeUpdateStatus.FAILED
    assert state.update_target_version == target_version
    assert not list(tmp_path.glob(".workgate.*"))


@pytest.mark.asyncio
async def test_control_managed_policy_updates_then_restarts_without_rotating_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "workgate"
    executable.write_text("runtime")
    _freeze_runtime(monkeypatch, executable)
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                default_workdir=tmp_path / "workspace",
                state_dir=tmp_path / "state",
            )
        ),
        managed_service=True,
        runtime_ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED,
    )
    assert runtime.profile_store is not None
    profile = ExecutorProfile(
        control_url="https://control.test",
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )
    runtime.profile_store.save(profile)

    async def update(**kwargs):
        assert kwargs["control_url"] == profile.control_url
        assert kwargs["target_version"] == "next-version"
        assert kwargs["store"] is runtime.runtime_state_store
        return executable

    monkeypatch.setattr(runtime_update, "apply_control_managed_update", update)
    policy = ExecutorHelloResponse(
        heartbeat_interval_s=10,
        offline_after_s=30,
        poll_timeout_s=1,
        required_workgate_version="next-version",
        runtime_update_required=True,
    )

    action = await runtime._handle_runtime_policy(policy)

    assert action is RuntimePolicyAction.RESTART
    assert runtime.profile_store.load() == profile


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


@pytest.mark.asyncio
async def test_failed_state_commit_preserves_previous_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_name = "linux-x86_64"
    target_version = "9.9.9"
    target = tmp_path / "workgate"
    original = b"old-runtime"
    target.write_bytes(original)
    target.chmod(0o700)
    _freeze_runtime(monkeypatch, target)

    store = _store(tmp_path)
    store.record_install(ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED)
    archive = _make_archive(target=target_name, version=target_version)
    digest = hashlib.sha256(archive).hexdigest()
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sha256"):
            return httpx.Response(200, text=digest + "\n")
        if request.url.path.endswith("/archive"):
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    def client_factory(**kwargs):
        return real_client(
            transport=httpx.MockTransport(handler),
            follow_redirects=bool(kwargs.get("follow_redirects", False)),
            timeout=kwargs.get("timeout"),
        )

    original_set_update = store.set_update

    def fail_commit(status, **kwargs):
        if status is not ExecutorRuntimeUpdateStatus.IDLE:
            return original_set_update(status, **kwargs)
        store.set_update = original_set_update
        raise RuntimeError("state commit failed")

    monkeypatch.setattr(runtime_update, "_target_name", lambda: target_name)
    monkeypatch.setattr(
        runtime_update, "_validate_executable", lambda *_a: None
    )
    monkeypatch.setattr(runtime_update.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(store, "set_update", fail_commit)

    with pytest.raises(RuntimeError, match="state commit failed"):
        await apply_control_managed_update(
            control_url="https://control.test",
            target_version=target_version,
            store=store,
        )

    assert target.read_bytes() == original
    state = store.load()
    assert state is not None
    assert state.update_status is ExecutorRuntimeUpdateStatus.FAILED
    assert state.update_target_version == target_version
    assert not list(tmp_path.glob(".workgate.*"))
