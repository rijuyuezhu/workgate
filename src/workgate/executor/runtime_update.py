"""Executor runtime ownership and control-managed release replacement."""

import hashlib
import os
import platform
import re
import secrets
import stat
import subprocess
import sys
import tarfile
from enum import StrEnum
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ..persistence import StateStore
from ..protocol.executor import ExecutorRuntimeOwnership

_RUNTIME_STATE_MAX_BYTES = 16 * 1024
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
_CHECKSUM_MAX_BYTES = 4 * 1024
_CHUNK_BYTES = 64 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BOOTSTRAP_PATH = "/executor/v1/bootstrap"


class ExecutorRuntimeUpdateStatus(StrEnum):
    """Executor-local runtime update lifecycle."""

    IDLE = "idle"
    REQUIRED = "required"
    PENDING = "pending"
    FAILED = "failed"


class ExecutorRuntimeState(BaseModel):
    """Small durable statement of runtime ownership and update health."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ownership: ExecutorRuntimeOwnership
    executable: str | None = Field(default=None, min_length=1, max_length=4096)
    update_status: ExecutorRuntimeUpdateStatus = (
        ExecutorRuntimeUpdateStatus.IDLE
    )
    update_target_version: str | None = Field(
        default=None, min_length=1, max_length=128
    )
    update_detail: str | None = Field(
        default=None, min_length=1, max_length=1000
    )


class ExecutorRuntimeStateStore:
    """Persist executor runtime ownership separately from pairing identity."""

    def __init__(self, state_store: StateStore) -> None:
        self.state_store = state_store

    def load(self) -> ExecutorRuntimeState | None:
        path = self.state_store.layout.executor_runtime_path
        with self.state_store.transaction(path):
            payload = self.state_store.read_json(
                path, max_bytes=_RUNTIME_STATE_MAX_BYTES
            )
        if payload is None:
            return None
        try:
            return ExecutorRuntimeState.model_validate(payload)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid executor runtime state: {path}"
            ) from exc

    def save(self, state: ExecutorRuntimeState) -> ExecutorRuntimeState:
        path = self.state_store.layout.executor_runtime_path
        with self.state_store.transaction(path):
            self.state_store.write_json(path, state.model_dump(mode="json"))
        return state

    def install_state(
        self,
        ownership: ExecutorRuntimeOwnership | None = None,
    ) -> ExecutorRuntimeState:
        selected = ownership or detect_runtime_ownership()
        executable = None
        if selected is ExecutorRuntimeOwnership.CONTROL_MANAGED:
            executable = _normalized_executable(current_frozen_executable())
            if executable is None:
                raise RuntimeError(
                    "control-managed runtime ownership requires a standalone executable"
                )
        return ExecutorRuntimeState(ownership=selected, executable=executable)

    def record_install(
        self,
        *,
        ownership: ExecutorRuntimeOwnership | None = None,
    ) -> ExecutorRuntimeState:
        """Record the runtime owner of a successfully installed service."""
        return self.save(self.install_state(ownership))

    def service_runtime_state(
        self,
        ownership: ExecutorRuntimeOwnership | None = None,
    ) -> ExecutorRuntimeState:
        """Return runtime state for the installed service process."""
        current = self.load()
        if ownership is not None:
            expected = self.install_state(ownership)
            if (
                current is None
                or current.ownership is not expected.ownership
                or current.executable != expected.executable
            ):
                return self.save(expected)
            return current
        if current is None:
            return self.record_install()
        if current.ownership is ExecutorRuntimeOwnership.CONTROL_MANAGED:
            executable = _normalized_executable(current_frozen_executable())
            if executable != current.executable:
                return self.record_install()
        return current

    def _require_state(self) -> ExecutorRuntimeState:
        current = self.load()
        if current is None:
            raise RuntimeError(
                "executor service runtime ownership is not recorded"
            )
        return current

    def set_update(
        self,
        status: ExecutorRuntimeUpdateStatus,
        *,
        target_version: str | None = None,
        detail: str | None = None,
    ) -> ExecutorRuntimeState:
        """Persist one local update state transition."""
        if status is ExecutorRuntimeUpdateStatus.IDLE:
            target_version = None
            detail = None
        elif target_version is None:
            raise ValueError(
                "non-idle runtime update state requires a target version"
            )
        current = self._require_state()
        next_state = current.model_copy(
            update={
                "update_status": status,
                "update_target_version": target_version,
                "update_detail": None if detail is None else detail[:1000],
            }
        )
        return current if next_state == current else self.save(next_state)


def _normalized_executable(path: Path | None) -> str | None:
    if path is None:
        return None
    candidate = Path(path).expanduser().absolute()
    return str(candidate)


def detect_runtime_ownership() -> ExecutorRuntimeOwnership:
    """Classify the current runtime conservatively when no explicit owner exists."""
    if bool(getattr(sys, "frozen", False)):
        return ExecutorRuntimeOwnership.SELF_CONTAINED
    try:
        current = Path(__file__).resolve()
        for parent in current.parents:
            if (parent / "pyproject.toml").is_file() and (
                parent / "src" / "workgate"
            ).is_dir():
                return ExecutorRuntimeOwnership.SOURCE
    except OSError:
        return ExecutorRuntimeOwnership.UNKNOWN
    return ExecutorRuntimeOwnership.EXTERNAL_PACKAGE


def current_frozen_executable() -> Path | None:
    """Return this process executable only for standalone frozen runtimes."""
    if not bool(getattr(sys, "frozen", False)):
        return None
    return Path(sys.executable).expanduser().absolute()


def _target_name() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux" and machine in {"x86_64", "amd64"}:
        return "linux-x86_64"
    if system == "Linux" and machine in {"aarch64", "arm64"}:
        return "linux-aarch64"
    if system == "Darwin" and machine in {"x86_64", "amd64"}:
        return "macos-x86_64"
    if system == "Darwin" and machine in {"aarch64", "arm64"}:
        return "macos-aarch64"
    raise RuntimeError(
        f"control-managed executor updates are unsupported on {system} {machine}"
    )


async def _download_checksum(client: httpx.AsyncClient, url: str) -> str:
    payload = bytearray()
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes(
            chunk_size=_CHECKSUM_MAX_BYTES + 1
        ):
            payload.extend(chunk)
            if len(payload) > _CHECKSUM_MAX_BYTES:
                raise RuntimeError(
                    "executor runtime checksum response is too large"
                )
    try:
        digest = payload.decode("utf-8").strip().lower()
    except UnicodeDecodeError as exc:
        raise RuntimeError("executor runtime checksum is invalid") from exc
    if _SHA256_RE.fullmatch(digest) is None:
        raise RuntimeError("executor runtime checksum is invalid")
    return digest


def _temp_path(directory: Path, suffix: str) -> Path:
    return (
        directory / f".workgate.{suffix}.{os.getpid()}.{secrets.token_hex(8)}"
    )


async def _download_archive(
    client: httpx.AsyncClient,
    url: str,
    path: Path,
    expected_sha256: str,
) -> None:
    digest = hashlib.sha256()
    total = 0
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(
                    chunk_size=_CHUNK_BYTES
                ):
                    total += len(chunk)
                    if total > _MAX_ARCHIVE_BYTES:
                        raise RuntimeError(
                            "executor runtime archive exceeds the size limit"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if digest.hexdigest() != expected_sha256:
        path.unlink(missing_ok=True)
        raise RuntimeError("executor runtime checksum mismatch")


def _stage_executable(
    archive: Path,
    staged: Path,
    *,
    target_name: str,
) -> None:
    expected_member = f"workgate-{target_name}/workgate"
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            member = bundle.getmember(expected_member)
            if not member.isfile() or member.size > _MAX_EXECUTABLE_BYTES:
                raise RuntimeError("executor runtime archive member is invalid")
            source = bundle.extractfile(member)
            if source is None:
                raise RuntimeError("executor runtime archive member is missing")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(staged, flags, 0o700)
            written = 0
            try:
                with os.fdopen(fd, "wb") as output:
                    while True:
                        chunk = source.read(_CHUNK_BYTES)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > _MAX_EXECUTABLE_BYTES:
                            raise RuntimeError(
                                "executor runtime executable exceeds the size limit"
                            )
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            except BaseException:
                staged.unlink(missing_ok=True)
                raise
            finally:
                source.close()
            if written != member.size:
                staged.unlink(missing_ok=True)
                raise RuntimeError(
                    "executor runtime archive member is truncated"
                )
    except (KeyError, tarfile.TarError, OSError) as exc:
        staged.unlink(missing_ok=True)
        raise RuntimeError("executor runtime archive is invalid") from exc


def _validate_executable(path: Path, version: str) -> None:
    result = subprocess.run(
        [str(path), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15.0,
    )
    expected = f"workgate {version}"
    if result.returncode != 0 or result.stdout.strip() != expected:
        raise RuntimeError(
            f"executor runtime validation failed for required version {version}"
        )


def _validate_replace_target(path: Path) -> None:
    if not path.is_absolute():
        raise RuntimeError("control-managed executor path must be absolute")
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(
            "control-managed executor runtime is missing"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError(
            "control-managed executor runtime must be a regular file"
        )


async def apply_control_managed_update(
    *,
    control_url: str,
    target_version: str,
    store: ExecutorRuntimeStateStore,
) -> Path:
    """Replace the explicit control-managed standalone executable transactionally."""
    state = store._require_state()
    if (
        state.ownership is not ExecutorRuntimeOwnership.CONTROL_MANAGED
        or state.executable is None
    ):
        raise RuntimeError("executor runtime is not control-managed")

    store.set_update(
        ExecutorRuntimeUpdateStatus.PENDING,
        target_version=target_version,
    )
    target = Path(state.executable)
    try:
        _validate_replace_target(target)
        target_name = _target_name()
        root = control_url.rstrip("/") + _BOOTSTRAP_PATH + f"/{target_name}"
        archive = _temp_path(target.parent, "archive")
        staged = _temp_path(target.parent, "next")
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(60.0, read=120.0),
            ) as client:
                checksum = await _download_checksum(client, root + "/sha256")
                await _download_archive(
                    client, root + "/archive", archive, checksum
                )
            _stage_executable(archive, staged, target_name=target_name)
            staged.chmod(0o700)
            _validate_executable(staged, target_version)

            # Runtime state is only local bookkeeping. Commit it before the
            # one filesystem mutation so any state failure leaves the old
            # executable untouched.
            store.set_update(ExecutorRuntimeUpdateStatus.IDLE)
            os.replace(staged, target)
            return target
        finally:
            archive.unlink(missing_ok=True)
            staged.unlink(missing_ok=True)
    except Exception as exc:
        store.set_update(
            ExecutorRuntimeUpdateStatus.FAILED,
            target_version=target_version,
            detail=str(exc).strip() or type(exc).__name__,
        )
        raise
