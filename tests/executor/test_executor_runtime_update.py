import hashlib
import io
import tarfile
import zipfile
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
    ExecutorRuntimeState,
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

    with pytest.raises(
        RuntimeError, match="requires a self-contained executable"
    ):
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

    def validate_staged(path: Path, version: str) -> None:
        assert version == target_version
        assert path.read_text().endswith(f"'workgate {target_version}'\n")

    monkeypatch.setattr(runtime_update, "_target_name", lambda: target_name)
    monkeypatch.setattr(
        runtime_update, "_validate_windows_service_executable", validate_staged
    )
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


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Linux", "x86_64", "linux-x86_64"),
        ("Linux", "aarch64", "linux-aarch64"),
        ("Darwin", "x86_64", "macos-x86_64"),
        ("Darwin", "arm64", "macos-aarch64"),
    ],
)
def test_target_name_maps_supported_release_targets(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    expected: str,
) -> None:
    monkeypatch.setattr(runtime_update.platform, "system", lambda: system)
    monkeypatch.setattr(runtime_update.platform, "machine", lambda: machine)

    assert runtime_update._target_name() == expected


def test_target_name_rejects_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_update.platform, "system", lambda: "Plan9")
    monkeypatch.setattr(runtime_update.platform, "machine", lambda: "mips")

    with pytest.raises(RuntimeError, match="unsupported on Plan9 mips"):
        runtime_update._target_name()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"x" * 4097, "too large"),
        (bytes([0xFF]), "checksum is invalid"),
        (b"not-a-sha256", "checksum is invalid"),
    ],
)
async def test_download_checksum_rejects_invalid_responses(
    payload: bytes, message: str
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=payload)
        )
    ) as client:
        with pytest.raises(RuntimeError, match=message):
            await runtime_update._download_checksum(
                client, "https://control.test/checksum"
            )


@pytest.mark.asyncio
async def test_download_archive_rejects_oversize_and_checksum_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oversized = tmp_path / "oversized.tar.gz"
    monkeypatch.setattr(runtime_update, "_MAX_ARCHIVE_BYTES", 3)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"four")
        )
    ) as client:
        with pytest.raises(RuntimeError, match="exceeds the size limit"):
            await runtime_update._download_archive(
                client,
                "https://control.test/archive",
                oversized,
                hashlib.sha256(b"four").hexdigest(),
            )
    assert not oversized.exists()

    monkeypatch.setattr(runtime_update, "_MAX_ARCHIVE_BYTES", 1024)
    mismatched = tmp_path / "mismatched.tar.gz"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"payload")
        )
    ) as client:
        with pytest.raises(RuntimeError, match="checksum mismatch"):
            await runtime_update._download_archive(
                client,
                "https://control.test/archive",
                mismatched,
                hashlib.sha256(b"other").hexdigest(),
            )
    assert not mismatched.exists()


def test_stage_and_validate_reject_invalid_runtime_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "empty.tar.gz"
    with tarfile.open(archive, "w:gz"):
        pass
    staged = tmp_path / "next"

    with pytest.raises(RuntimeError, match="archive is invalid"):
        runtime_update._stage_executable(
            archive, staged, target_name="linux-x86_64"
        )
    assert not staged.exists()

    monkeypatch.setattr(
        runtime_update.subprocess,
        "run",
        lambda *_args, **_kwargs: runtime_update.subprocess.CompletedProcess(
            args=["workgate", "--version"],
            returncode=0,
            stdout="workgate wrong\\n",
            stderr="",
        ),
    )
    with pytest.raises(RuntimeError, match="validation failed"):
        runtime_update._validate_executable(tmp_path / "workgate", "9.9.9")


def test_replace_target_and_update_authority_fail_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="must be absolute"):
        runtime_update._validate_replace_target(Path("relative-workgate"))

    with pytest.raises(RuntimeError, match="runtime is missing"):
        runtime_update._validate_replace_target(tmp_path / "missing")

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(RuntimeError, match="must be a regular file"):
        runtime_update._validate_replace_target(directory)


@pytest.mark.asyncio
async def test_control_update_refuses_non_control_managed_state(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.save(
        ExecutorRuntimeState(
            ownership=ExecutorRuntimeOwnership.EXTERNAL_PACKAGE
        )
    )

    with pytest.raises(RuntimeError, match="not control-managed"):
        await apply_control_managed_update(
            control_url="https://control.test",
            target_version="9.9.9",
            store=store,
        )


@pytest.mark.parametrize("machine", ["AMD64", "x86_64"])
def test_target_name_windows_x86_64(
    monkeypatch: pytest.MonkeyPatch, machine: str
) -> None:
    monkeypatch.setattr(runtime_update.platform, "system", lambda: "Windows")
    monkeypatch.setattr(runtime_update.platform, "machine", lambda: machine)
    assert runtime_update._target_name() == "windows-x86_64"


def test_stage_windows_runtime_extracts_only_exact_member(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "release.zip"
    expected = b"MZ" + b"versioned" * 32
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("workgate-windows-x86_64/workgatew.exe", expected)
        bundle.writestr("../../malicious.exe", b"MALICIOUS")
    staged = tmp_path / "staged.exe"
    runtime_update._stage_executable(
        archive, staged, target_name="windows-x86_64"
    )
    assert staged.read_bytes() == expected
    assert not (tmp_path / "malicious.exe").exists()


def test_stage_windows_runtime_rejects_invalid_zip_and_missing_member(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "release.zip"
    archive.write_bytes(b"not a ZIP archive")
    with pytest.raises(RuntimeError, match="archive is invalid"):
        runtime_update._stage_executable(
            archive, tmp_path / "staged.exe", target_name="windows-x86_64"
        )
    assert not (tmp_path / "staged.exe").exists()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("workgate-windows-x86_64/other.exe", b"NOT_WORKGATE")
    with pytest.raises(RuntimeError, match="archive is invalid"):
        runtime_update._stage_executable(
            archive, tmp_path / "staged.exe", target_name="windows-x86_64"
        )
    assert not (tmp_path / "staged.exe").exists()


def test_stage_windows_runtime_enforces_decompressed_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(
        archive, "w", compression=zipfile.ZIP_DEFLATED
    ) as bundle:
        bundle.writestr("workgate-windows-x86_64/workgatew.exe", b"A" * 64)
    monkeypatch.setattr(runtime_update, "_MAX_EXECUTABLE_BYTES", 32)
    with pytest.raises(RuntimeError, match="archive member is invalid"):
        runtime_update._stage_executable(
            archive, tmp_path / "staged.exe", target_name="windows-x86_64"
        )
    assert not (tmp_path / "staged.exe").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("activation_fails", [False, True])
async def test_windows_managed_update_switches_verified_versioned_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, activation_fails: bool
) -> None:
    target_name = "windows-x86_64"
    target_version = "9.9.9"
    target = tmp_path / "workgate.exe"
    target.write_bytes(b"old-image-locked")
    _freeze_runtime(monkeypatch, target)
    store = _store(tmp_path)
    store.record_install(ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED)
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as bundle:
        bundle.writestr(
            f"workgate-{target_name}/workgatew.exe", b"MZnew-verified-image"
        )
    archive = archive_bytes.getvalue()
    digest = hashlib.sha256(archive).hexdigest()
    real_client = httpx.AsyncClient

    def client_factory(**_kwargs):
        def serve(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/sha256"):
                return httpx.Response(200, text=digest)
            if request.url.path.endswith("/archive"):
                return httpx.Response(200, content=archive)
            return httpx.Response(404)

        return real_client(transport=httpx.MockTransport(serve))

    monkeypatch.setattr(runtime_update, "_target_name", lambda: target_name)
    monkeypatch.setattr(runtime_update.httpx, "AsyncClient", client_factory)

    def validate(path: Path, version: str) -> None:
        assert path.suffix == ".exe"
        assert path.read_bytes() == b"MZnew-verified-image"
        assert version == target_version

    monkeypatch.setattr(
        runtime_update, "_validate_windows_service_executable", validate
    )
    calls = []

    def activate(old: Path, new: Path) -> None:
        assert old == target
        assert new != target
        assert new.is_file() and new.suffix == ".exe"
        state = store.load()
        assert state is not None and state.executable == str(new)
        calls.append((old, new))
        if activation_fails:
            raise RuntimeError("Task Scheduler activation refused")

    if activation_fails:
        with pytest.raises(RuntimeError, match="activation refused"):
            await apply_control_managed_update(
                control_url="https://control.test",
                target_version=target_version,
                store=store,
                windows_activate=activate,
            )
        assert target.read_bytes() == b"old-image-locked"
        state = store.load()
        assert state is not None
        assert state.executable == str(target)
        assert state.update_status is ExecutorRuntimeUpdateStatus.FAILED
        return

    published = await apply_control_managed_update(
        control_url="https://control.test",
        target_version=target_version,
        store=store,
        windows_activate=activate,
    )
    assert calls == [(target, published)]
    assert target.read_bytes() == b"old-image-locked"
    assert published.read_bytes() == b"MZnew-verified-image"
    state = store.load()
    assert state is not None
    assert state.executable == str(published)
    assert state.update_status is ExecutorRuntimeUpdateStatus.IDLE


@pytest.mark.asyncio
async def test_windows_managed_update_rejects_missing_task_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "workgate.exe"
    target.write_bytes(b"old-image-locked")
    _freeze_runtime(monkeypatch, target)
    store = _store(tmp_path)
    store.record_install(ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED)
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as bundle:
        bundle.writestr("workgate-windows-x86_64/workgatew.exe", b"MZverified")
    digest = hashlib.sha256(archive_bytes.getvalue()).hexdigest()
    real_client = httpx.AsyncClient

    def client_factory(**_kwargs):
        return real_client(
            transport=httpx.MockTransport(
                lambda request: (
                    httpx.Response(200, text=digest)
                    if request.url.path.endswith("sha256")
                    else httpx.Response(200, content=archive_bytes.getvalue())
                )
            )
        )

    monkeypatch.setattr(
        runtime_update, "_target_name", lambda: "windows-x86_64"
    )
    monkeypatch.setattr(runtime_update.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(
        runtime_update,
        "_validate_windows_service_executable",
        lambda *_args: None,
    )
    with pytest.raises(RuntimeError, match="owned Task Scheduler"):
        await apply_control_managed_update(
            control_url="https://control.test",
            target_version="9.9.9",
            store=store,
        )
    assert target.read_bytes() == b"old-image-locked"
    state = store.load()
    assert state is not None
    assert state.update_status is ExecutorRuntimeUpdateStatus.FAILED
    assert state.executable == str(target)


def test_windowless_windows_version_probe_matches_and_cleans_output(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "workgatew.exe"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[2]).write_text('workgate 9.9.9\\n')\n"
    )
    binary.chmod(0o700)
    runtime_update._validate_windows_service_executable(binary, "9.9.9")
    with pytest.raises(RuntimeError, match="required version 9.9.8"):
        runtime_update._validate_windows_service_executable(binary, "9.9.8")
    assert list(tmp_path.glob(".workgate.version.*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage_fails", [False, True])
async def test_managed_source_policy_stages_only_without_switching(
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
    policy = ExecutorHelloResponse(
        heartbeat_interval_s=10,
        offline_after_s=30,
        poll_timeout_s=1,
        required_workgate_version="5.0.0a99",
        runtime_update_required=True,
    )

    action = await runtime._handle_runtime_policy(policy)

    assert action is RuntimePolicyAction.BLOCK
    assert staged == ["5.0.0a99"]
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
