"""Server-side state and coordination for remote workers."""

import asyncio
import contextlib
import json
import math
import os
import re
import secrets
import shlex
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from ..audit import audit
from ..config.settings import Settings, get_settings
from ..persistence import StateStore, get_state_store
from ..schemas.result_models.remote import (
    RemoteInviteOutput,
    RemoteListMachinesOutput,
    RemoteReconnectCommandOutput,
    RemoteRenameMachineOutput,
    RemoteRevokeMachineOutput,
)
from .bundle import worker_bundle_manifest
from .constants import (
    REMOTE_JOIN_PATH,
    REMOTE_WORKER_MANIFEST_PATH,
    REMOTE_WORKER_POLL_PROTOCOL_VERSION,
    REMOTE_WORKER_RUNTIME_KIND,
    REMOTE_WORKER_RUNTIME_PROTOCOL_VERSION,
)
from .responses import _ok

MAX_REMOTE_INVITES = 1_024
MAX_REMOTE_MACHINE_NAME_LENGTH = 128
REMOTE_WORKER_REGISTRY_VERSION = 1
_WORKER_PROFILE_ID_RE = re.compile(r"p_[A-Za-z0-9_-]{8,64}")
_WORKER_LAUNCHER_PATH_MAX_BYTES = 4_096

_WORKER_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class WorkerRuntimeReport(TypedDict):
    """Validated managed-worker runtime identity shared by register/poll flows."""

    protocol_version: int
    """Worker/runtime compatibility protocol version."""
    runtime_kind: str
    """Reported runtime class such as managed bundle or unmanaged source."""
    worker_version: str
    """workgate version reported by the worker process."""
    bundle_version: str
    """Installed managed bundle version, when one is active."""
    bundle_sha256: str
    """SHA-256 digest of the active managed bundle, when one is active."""


class WorkerRuntimeInfo(TypedDict):
    """Stable runtime metadata projected into one worker's extensible info map."""

    runtime_protocol_version: int
    """Validated runtime compatibility protocol version."""
    runtime_kind: str
    """Validated worker runtime class."""
    workgate_version: str
    """workgate version reported by the worker."""
    worker_bundle_version: str
    """Validated managed bundle version."""
    worker_bundle_sha256: str
    """Validated managed bundle SHA-256 digest."""


class WorkerUpgradeInstruction(TypedDict):
    """Controller-issued instruction describing the currently required bundle."""

    required: bool
    """Whether the worker must install the advertised bundle before taking jobs."""
    version: str
    """Advertised managed bundle version."""
    sha256: str
    """Expected SHA-256 digest of the advertised bundle."""
    manifest_path: str
    """Controller-relative path used to fetch the signed bundle manifest/archive."""


class WorkerRegistrationResponse(TypedDict):
    """Stable enrollment/resume response consumed by managed workers."""

    token: str
    """Opaque bearer token assigned to the registered worker."""
    name: str
    """Controller-resolved stable machine name."""
    poll_interval_s: int
    """Recommended idle polling cadence in seconds."""
    poll_timeout_s: int
    """Maximum long-poll duration in seconds."""
    heartbeat_interval_s: int
    """Heartbeat cadence used while a remote job is running."""
    upgrade: WorkerUpgradeInstruction
    """Bundle compatibility instruction evaluated immediately after registration."""


class RemoteQueuedJob(TypedDict):
    """One controller-created remote tool call waiting in a worker queue."""

    id: str
    """Opaque controller-issued remote job identifier."""
    tool: str
    """Remote dispatch operation name."""
    args: dict[str, Any]
    """Validated or transport-safe operation arguments."""
    expires_at: float
    """Unix timestamp after which the worker must not begin execution."""


class WorkerHeartbeatResponse(TypedDict):
    """Acknowledgement for a managed worker heartbeat."""

    accepted: bool
    """Whether the controller accepted and recorded the heartbeat."""
    name: str
    """Current controller-side machine name for the worker."""


def _utc() -> float:
    """Return a Unix timestamp used for invite and worker bookkeeping."""
    return time.time()


def _heartbeat_interval_s(settings: Settings) -> int:
    """Return a bounded heartbeat cadence for workers executing long jobs."""
    return max(5, min(settings.remote_poll_timeout_s // 2, 30))


def _validate_machine_name(value: str) -> str:
    """Normalize and validate one public remote-machine name."""
    name = value.strip()
    if not name:
        raise ValueError("machine name is required")
    if len(name) > MAX_REMOTE_MACHINE_NAME_LENGTH:
        raise ValueError(
            f"machine name exceeds {MAX_REMOTE_MACHINE_NAME_LENGTH} characters"
        )
    if any(
        ord(character) < 32 or character in {"/", "\\"} for character in name
    ):
        raise ValueError("machine name contains unsupported characters")
    return name


def _reported_text(payload: dict[str, Any], key: str, *, required: bool) -> str:
    """Validate one bounded worker-reported text field."""
    value = payload.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"worker poll {key} must be a string")
    if required and not value:
        raise ValueError(f"worker poll {key} is required")
    if len(value) > 128 or any(ord(character) < 32 for character in value):
        raise ValueError(f"worker poll {key} is invalid")
    return value


class WorkerRuntimeCompatibilityError(ValueError):
    """Raised when a worker is not running a supported managed bundle."""


class RemoteManagerClosedError(RuntimeError):
    """Raised when remote work is attempted after manager shutdown begins."""


def _validate_runtime_report(payload: dict[str, Any]) -> WorkerRuntimeReport:
    """Require one complete managed-bundle report before enrollment or resume."""
    raw = payload.get("runtime")
    if not isinstance(raw, dict):
        raise WorkerRuntimeCompatibilityError(
            "managed remote worker runtime required; use the generated invite command"
        )
    protocol = raw.get("protocol_version")
    if (
        isinstance(protocol, bool)
        or not isinstance(protocol, int)
        or protocol != REMOTE_WORKER_RUNTIME_PROTOCOL_VERSION
    ):
        raise WorkerRuntimeCompatibilityError(
            "remote worker runtime protocol is unsupported; use a current generated invite command"
        )
    runtime_kind = _reported_text(raw, "runtime_kind", required=True)
    if runtime_kind != REMOTE_WORKER_RUNTIME_KIND:
        raise WorkerRuntimeCompatibilityError(
            "managed remote worker runtime required; manually installed source is not accepted"
        )
    worker_version = _reported_text(raw, "worker_version", required=True)
    bundle_version = _reported_text(raw, "bundle_version", required=True)
    digest = _reported_text(raw, "bundle_sha256", required=True).lower()
    if not _WORKER_DIGEST_RE.fullmatch(digest):
        raise WorkerRuntimeCompatibilityError(
            "remote worker managed bundle digest is invalid"
        )
    return {
        "protocol_version": protocol,
        "runtime_kind": runtime_kind,
        "worker_version": worker_version,
        "bundle_version": bundle_version,
        "bundle_sha256": digest,
    }


def _runtime_info(report: WorkerRuntimeReport) -> WorkerRuntimeInfo:
    return {
        "runtime_protocol_version": report["protocol_version"],
        "runtime_kind": report["runtime_kind"],
        "workgate_version": report["worker_version"],
        "worker_bundle_version": report["bundle_version"],
        "worker_bundle_sha256": report["bundle_sha256"],
    }


def _format_worker_reconnect_command(argv: list[str], *, platform: str) -> str:
    """Format validated reconnect arguments for the worker's command shell."""
    return (
        subprocess.list2cmdline(argv)
        if platform == "win32"
        else shlex.join(argv)
    )


def _worker_reconnect_metadata(
    info: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Validate worker-local launcher metadata and build a shell-safe command."""
    raw_profile_id = info.get("profile_id")
    raw_launcher_path = info.get("launcher_path")
    if raw_profile_id is not None and not isinstance(raw_profile_id, str):
        return None, None
    if raw_launcher_path is not None and not isinstance(raw_launcher_path, str):
        return None, None
    if not raw_profile_id and not raw_launcher_path:
        return None, None
    if not raw_profile_id or not _WORKER_PROFILE_ID_RE.fullmatch(
        raw_profile_id
    ):
        return None, None
    if not raw_launcher_path:
        return None, None
    if len(
        raw_launcher_path.encode("utf-8")
    ) > _WORKER_LAUNCHER_PATH_MAX_BYTES or any(
        ord(character) < 32 for character in raw_launcher_path
    ):
        return None, None
    platform = info.get("platform")
    return raw_profile_id, _format_worker_reconnect_command(
        [raw_launcher_path, raw_profile_id],
        platform=platform if isinstance(platform, str) else "",
    )


def _validate_poll_report(
    payload: dict[str, Any] | None,
) -> WorkerRuntimeReport:
    """Require a complete managed-runtime report before delivering jobs."""
    if not payload:
        raise WorkerRuntimeCompatibilityError(
            "managed remote worker poll report required; use a current generated invite command"
        )
    protocol = payload.get("protocol_version")
    if isinstance(protocol, bool) or not isinstance(protocol, int):
        raise ValueError("worker poll protocol_version must be an integer")
    if protocol != REMOTE_WORKER_POLL_PROTOCOL_VERSION:
        raise WorkerRuntimeCompatibilityError(
            "worker poll protocol_version is unsupported; update the managed worker runtime"
        )

    runtime_kind = _reported_text(payload, "runtime_kind", required=True)
    if runtime_kind != REMOTE_WORKER_RUNTIME_KIND:
        raise WorkerRuntimeCompatibilityError(
            "managed remote worker runtime required; manually installed source is not accepted"
        )
    worker_version = _reported_text(payload, "worker_version", required=True)
    bundle_version = _reported_text(payload, "bundle_version", required=True)
    digest = _reported_text(payload, "bundle_sha256", required=True).lower()
    if not _WORKER_DIGEST_RE.fullmatch(digest):
        raise ValueError("worker poll bundle_sha256 is invalid")
    return {
        "protocol_version": protocol,
        "runtime_kind": runtime_kind,
        "worker_version": worker_version,
        "bundle_version": bundle_version,
        "bundle_sha256": digest,
    }


def _upgrade_instruction(*, required: bool) -> WorkerUpgradeInstruction:
    """Return a non-sensitive instruction bound to the current bundle digest."""
    manifest = worker_bundle_manifest()
    return {
        "required": required,
        "version": manifest["bundle_version"],
        "sha256": manifest["sha256"],
        "manifest_path": REMOTE_WORKER_MANIFEST_PATH,
    }


def _runtime_upgrade(report: WorkerRuntimeReport) -> WorkerUpgradeInstruction:
    """Return the current bundle instruction for one managed runtime report."""
    return _upgrade_instruction(
        required=(report["bundle_sha256"] != worker_bundle_manifest()["sha256"])
    )


@dataclass
class RemoteInvite:
    """One-time enrollment token for a remote worker."""

    code: str
    """Opaque one-time enrollment code."""
    name: str | None
    """Optional machine name bound to the invite."""
    workdir: str | None
    """Optional worker-side starting directory requested by the invite."""
    expires_at: float
    """Unix timestamp after which enrollment must be rejected."""
    used: bool = False
    """Whether this invite has already enrolled a worker."""


@dataclass
class RemoteWorker:
    """Registered remote worker state."""

    name: str
    """Stable public machine name."""
    token: str
    """Opaque bearer credential used by this worker."""
    workdir: str | None = None
    """Last worker-side directory reported during registration or resume."""
    created_at: float = field(default_factory=_utc)
    """Unix timestamp when the registration was created."""
    last_seen: float = field(default_factory=_utc)
    """Unix timestamp of the latest poll, heartbeat, or result submission."""
    status: str = "online"
    """Cached connection status used for inventory output."""
    capabilities: list[str] = field(default_factory=list)
    """Worker tool categories advertised at registration or resume."""
    info: dict[str, Any] = field(default_factory=dict)
    """Worker platform and environment metadata."""
    queue: asyncio.Queue[Mapping[str, Any]] = field(
        default_factory=asyncio.Queue
    )
    """Control-side delivery queue for jobs assigned to this worker."""


class RemoteManager:
    """Coordinate remote worker enrollment, polling, jobs, and persisted identity."""

    def __init__(
        self,
        settings_provider: Callable[[], Settings] = get_settings,
        *,
        state_store: StateStore | None = None,
    ) -> None:
        self._settings_provider = settings_provider
        self._state_store = state_store
        self.invites: dict[str, RemoteInvite] = {}
        self.workers: dict[str, RemoteWorker] = {}
        self.tokens: dict[str, str] = {}
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.pending_machines: dict[str, str] = {}
        self.cancelled_jobs: dict[str, float] = {}
        self._enrollment_lock: asyncio.Lock | None = None
        self._poll_waiters: set[asyncio.Task[Any]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._state_lock = threading.RLock()
        self._registry_loaded_path: Path | None = None

    async def start(self) -> None:
        """Bind loop-owned primitives and load durable workers on the owning loop."""
        if self._closed:
            raise RemoteManagerClosedError("remote manager is closed")
        loop = asyncio.get_running_loop()
        if self._loop is not None:
            if self._loop is not loop:
                raise RuntimeError("remote manager cannot span event loops")
            return
        self._loop = loop
        self._enrollment_lock = asyncio.Lock()
        try:
            with self._state_lock:
                self._load_registry_unlocked()
        except BaseException:
            self._enrollment_lock = None
            self._loop = None
            raise

    async def aclose(self) -> None:
        """Stop admission and cancel manager-owned pending calls and poll waiters."""
        if self._closed:
            return
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError(
                "remote manager must close on its owning event loop"
            )
        self._closed = True
        with self._state_lock:
            pending = tuple(self.pending.values())
            self.pending.clear()
            self.pending_machines.clear()
            self.invites.clear()
            self.cancelled_jobs.clear()
            for worker in self.workers.values():
                while True:
                    try:
                        worker.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
        waiters = tuple(self._poll_waiters)
        self._poll_waiters.clear()
        for future in pending:
            if not future.done():
                future.cancel()
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()
        if waiters:
            await asyncio.gather(*waiters, return_exceptions=True)

    async def _ensure_started(self) -> asyncio.Lock:
        """Start on first direct use and return the owning enrollment lock."""
        await self.start()
        lock = self._enrollment_lock
        if lock is None:  # pragma: no cover - guarded by start()
            raise RuntimeError("remote manager enrollment lock is unavailable")
        return lock

    def _require_not_closed(self) -> None:
        """Reject synchronous mutations after shutdown has stopped admission."""
        if self._closed:
            raise RemoteManagerClosedError("remote manager is closed")

    def _settings(self) -> Settings:
        """Return the settings dependency supplied at composition time."""
        return self._settings_provider()

    def _registry_path(self) -> Path:
        state_store = self._state_store or get_state_store()
        return state_store.layout.remote_workers_path

    def _registry_backup_path(self) -> Path:
        path = self._registry_path()
        return path.with_name(path.name + ".bak")

    @staticmethod
    def _read_registry(path: Path) -> list[dict[str, Any]]:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("version") != REMOTE_WORKER_REGISTRY_VERSION
        ):
            raise ValueError(
                f"unsupported or invalid remote worker registry: {path}"
            )
        rows = data.get("workers")
        if not isinstance(rows, list):
            raise ValueError(
                f"remote worker registry workers field is invalid: {path}"
            )
        return [row for row in rows if isinstance(row, dict)]

    def _load_registry_unlocked(self) -> None:
        """Load persisted workers once per active state directory."""
        path = self._registry_path()
        if self._registry_loaded_path == path:
            return
        backup_path = self._registry_backup_path()
        rows: list[dict[str, Any]] = []
        recovered = False
        if path.exists():
            try:
                rows = self._read_registry(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                audit(
                    "remote_worker_registry_unreadable",
                    path=str(path),
                    error=repr(exc),
                )
                if backup_path.exists():
                    try:
                        rows = self._read_registry(backup_path)
                    except (
                        OSError,
                        ValueError,
                        json.JSONDecodeError,
                    ) as backup_exc:
                        audit(
                            "remote_worker_registry_backup_unreadable",
                            path=str(backup_path),
                            error=repr(backup_exc),
                        )
                    else:
                        recovered = True
                        audit(
                            "remote_worker_registry_recovered",
                            path=str(path),
                            backup_path=str(backup_path),
                        )
                if not recovered:
                    raise RuntimeError(
                        "Remote worker registry is unreadable and no valid backup is available; refusing to reset it"
                    ) from exc
        elif backup_path.exists():
            try:
                rows = self._read_registry(backup_path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "Remote worker registry backup is unreadable; refusing to reset it"
                ) from exc
            recovered = True

        workers: dict[str, RemoteWorker] = {}
        tokens: dict[str, str] = {}
        for item in rows:
            name = str(item.get("name") or "").strip()
            access = str(item.get("access") or item.get("token") or "").strip()
            try:
                name = _validate_machine_name(name)
            except ValueError:
                continue
            if not access or name in workers or access in tokens:
                continue
            workers[name] = RemoteWorker(
                name=name,
                token=access,
                workdir=str(item.get("workdir") or ""),
                created_at=float(item.get("created_at") or _utc()),
                last_seen=0.0,
                status="offline",
                capabilities=list(item.get("capabilities") or []),
                info=dict(item.get("info") or {}),
            )
            tokens[access] = name

        self.workers = workers
        self.tokens = tokens
        self._registry_loaded_path = path
        if recovered:
            self._save_registry_unlocked()

    def _save_registry_unlocked(self) -> None:
        """Atomically persist primary and backup worker registries."""
        path = self._registry_path()
        backup_path = self._registry_backup_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": REMOTE_WORKER_REGISTRY_VERSION,
            "workers": [
                {
                    "name": worker.name,
                    "access": worker.token,
                    "workdir": worker.workdir,
                    "created_at": worker.created_at,
                    "capabilities": worker.capabilities,
                    "info": worker.info,
                }
                for worker in sorted(
                    self.workers.values(), key=lambda item: item.name
                )
            ],
        }
        payload = json.dumps(data, indent=2, sort_keys=True)
        temporary_paths = [
            path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp"),
            backup_path.with_name(f"{backup_path.name}.{uuid.uuid4().hex}.tmp"),
        ]
        try:
            for temporary in temporary_paths:
                temporary.write_text(payload, encoding="utf-8")
                with contextlib.suppress(OSError):
                    temporary.chmod(0o600)
            os.replace(temporary_paths[0], path)
            os.replace(temporary_paths[1], backup_path)
            with contextlib.suppress(OSError):
                path.chmod(0o600)
                backup_path.chmod(0o600)
        finally:
            for temporary in temporary_paths:
                temporary.unlink(missing_ok=True)

    def _join_url(self) -> str:
        return self._settings().resolved_base_url + REMOTE_JOIN_PATH

    def _prune_invites_unlocked(self) -> None:
        now = _utc()
        self.invites = {
            code: invite
            for code, invite in self.invites.items()
            if not invite.used and invite.expires_at >= now
        }

    def _prune_cancelled_jobs_unlocked(self) -> None:
        now = _utc()
        ttl = max(60, int(self._settings().remote_job_timeout_s))
        self.cancelled_jobs = {
            job_id: cancelled_at
            for job_id, cancelled_at in self.cancelled_jobs.items()
            if now - cancelled_at <= ttl
        }
        cap = max(64, int(self._settings().remote_max_pending_jobs) * 4)
        if len(self.cancelled_jobs) > cap:
            newest = sorted(
                self.cancelled_jobs.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:cap]
            self.cancelled_jobs = dict(newest)

    def _cancel_job_unlocked(self, job_id: str) -> None:
        future = self.pending.pop(job_id, None)
        self.pending_machines.pop(job_id, None)
        self.cancelled_jobs[job_id] = _utc()
        if future and not future.done():
            future.cancel()
        self._prune_cancelled_jobs_unlocked()

    async def create_invite(
        self,
        name: str | None = None,
        workdir: str | None = None,
        ttl_s: int | None = None,
    ) -> RemoteInviteOutput:
        """Create one bounded, one-time remote-worker enrollment invite."""
        enrollment_lock = await self._ensure_started()
        settings = self._settings()
        ttl = max(60, min(ttl_s or settings.remote_invite_ttl_s, 24 * 3600))
        normalized_name = _validate_machine_name(name) if name else None
        code = "workgate_inv_" + secrets.token_urlsafe(24)
        invite = RemoteInvite(
            code=code,
            name=normalized_name,
            workdir=workdir,
            expires_at=_utc() + ttl,
        )
        async with enrollment_lock:
            with self._state_lock:
                self._load_registry_unlocked()
                self._prune_invites_unlocked()
                if len(self.invites) >= MAX_REMOTE_INVITES:
                    raise RuntimeError("Too many pending remote invites")
                self.invites[code] = invite
        command = f"curl -fsSL {shlex.quote(self._join_url())} | bash -s -- --invite {shlex.quote(code)}"
        if normalized_name:
            command += f" --name {shlex.quote(normalized_name)}"
        if workdir:
            command += f" --workdir {shlex.quote(workdir)}"
        return RemoteInviteOutput(
            code=code,
            name=normalized_name,
            workdir=workdir,
            expires_at=invite.expires_at,
            ttl_s=ttl,
            join_url=self._join_url(),
            command=command,
        )

    async def register_worker(
        self, payload: dict[str, Any]
    ) -> WorkerRegistrationResponse:
        """Consume an invite and persist a new worker registration."""
        enrollment_lock = await self._ensure_started()
        runtime_report = _validate_runtime_report(payload)
        code = str(payload.get("invite") or "")
        requested_name = str(payload.get("name") or "").strip() or None
        async with enrollment_lock:
            with self._state_lock:
                self._load_registry_unlocked()
                self._prune_invites_unlocked()
                invite = self.invites.get(code)
                if not invite:
                    raise ValueError("invalid invite code")
                if invite.used:
                    raise ValueError("invite code has already been used")
                if invite.expires_at < _utc():
                    raise ValueError("invite code has expired")
                name = _validate_machine_name(
                    requested_name
                    or invite.name
                    or self._default_machine_name_unlocked(payload)
                )
                if (
                    invite.name
                    and requested_name
                    and requested_name != invite.name
                ):
                    raise ValueError(
                        f"invite is bound to machine name {invite.name!r}"
                    )
                if name in self.workers:
                    raise ValueError(f"machine name already exists: {name}")
                token = "workgate_wk_" + secrets.token_urlsafe(32)
                info = dict(payload.get("info") or {})
                info.update(_runtime_info(runtime_report))
                worker = RemoteWorker(
                    name=name,
                    token=token,
                    workdir=str(payload.get("workdir") or invite.workdir or ""),
                    capabilities=list(payload.get("capabilities") or []),
                    info=info,
                )
                self.workers[name] = worker
                self.tokens[token] = name
                invite.used = True
                self.invites.pop(code, None)
                self._save_registry_unlocked()
        audit("remote_worker_registered", machine=name)
        return {
            "token": token,
            "name": name,
            "poll_interval_s": 0,
            "poll_timeout_s": self._settings().remote_poll_timeout_s,
            "heartbeat_interval_s": _heartbeat_interval_s(self._settings()),
            "upgrade": _runtime_upgrade(runtime_report),
        }

    async def resume_worker(
        self, token: str, payload: dict[str, Any]
    ) -> WorkerRegistrationResponse:
        """Refresh a persisted worker registration using its bearer identity."""
        enrollment_lock = await self._ensure_started()
        runtime_report = _validate_runtime_report(payload)
        async with enrollment_lock:
            with self._state_lock:
                self._load_registry_unlocked()
                # The bearer token is canonical; the reported name may be stale
                # after an administrator renames this registration.
                worker = self._worker_by_token_unlocked(token)
                worker.status = "online"
                worker.last_seen = _utc()
                worker.workdir = str(
                    payload.get("workdir") or worker.workdir or ""
                )
                worker.capabilities = list(
                    payload.get("capabilities") or worker.capabilities
                )
                worker.info = dict(payload.get("info") or worker.info)
                worker.info.update(_runtime_info(runtime_report))
                self._save_registry_unlocked()
                name = worker.name
        audit("remote_worker_resumed", machine=name)
        return {
            "token": token,
            "name": name,
            "poll_interval_s": 0,
            "poll_timeout_s": self._settings().remote_poll_timeout_s,
            "heartbeat_interval_s": _heartbeat_interval_s(self._settings()),
            "upgrade": _runtime_upgrade(runtime_report),
        }

    def _default_machine_name_unlocked(self, payload: dict[str, Any]) -> str:
        raw_info = payload.get("info")
        info = raw_info if isinstance(raw_info, dict) else {}
        user = info.get("user") or os.getenv("USER") or "user"
        host = info.get("hostname") or "remote"
        base = _validate_machine_name(f"{user}@{host}")
        if base not in self.workers:
            return base
        index = 2
        while f"{base}-{index}" in self.workers:
            index += 1
        return f"{base}-{index}"

    async def poll(
        self,
        token: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Negotiate runtime state, then long-poll for the next live job."""
        await self.start()
        report = _validate_poll_report(payload)
        configured_poll_timeout_s = float(
            self._settings().remote_poll_timeout_s
        )
        effective_poll_timeout_s = configured_poll_timeout_s
        try:
            worker_poll_timeout_s = float(
                (payload or {}).get("poll_timeout_s") or 0
            )
        except TypeError, ValueError:
            worker_poll_timeout_s = 0.0
        if math.isfinite(worker_poll_timeout_s) and worker_poll_timeout_s > 0:
            effective_poll_timeout_s = min(
                configured_poll_timeout_s, worker_poll_timeout_s
            )
        upgrade = _runtime_upgrade(report)
        with self._state_lock:
            self._load_registry_unlocked()
            worker = self._worker_by_token_unlocked(token)
            worker.status = "online"
            worker.last_seen = _utc()
            info_updates = {
                "poll_protocol_version": report["protocol_version"],
                **_runtime_info(report),
            }
            if any(
                worker.info.get(key) != value
                for key, value in info_updates.items()
            ):
                worker.info.update(info_updates)
                self._save_registry_unlocked()
        if upgrade["required"]:
            return {
                "job": None,
                "upgrade": upgrade,
                "poll_timeout_s": configured_poll_timeout_s,
            }

        loop = asyncio.get_running_loop()
        deadline = loop.time() + effective_poll_timeout_s
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                response: dict[str, Any] = {
                    "job": None,
                    "heartbeat": True,
                    "poll_timeout_s": configured_poll_timeout_s,
                    "upgrade": upgrade,
                }
                return response
            try:
                poll_task = asyncio.current_task()
                if poll_task is not None:
                    self._poll_waiters.add(poll_task)
                try:
                    job = await asyncio.wait_for(
                        worker.queue.get(), timeout=remaining
                    )
                finally:
                    if poll_task is not None:
                        self._poll_waiters.discard(poll_task)
            except TimeoutError:
                response = {
                    "job": None,
                    "heartbeat": True,
                    "poll_timeout_s": configured_poll_timeout_s,
                    "upgrade": upgrade,
                }
                return response
            except asyncio.CancelledError:
                if self._closed:
                    raise RemoteManagerClosedError(
                        "remote manager is closed"
                    ) from None
                raise
            job_id = str(job.get("id") or "")
            with self._state_lock:
                self._prune_cancelled_jobs_unlocked()
                if job_id in self.cancelled_jobs:
                    self.cancelled_jobs.pop(job_id, None)
                    continue
                expires_at = float(job.get("expires_at") or 0)
                if expires_at and expires_at < _utc():
                    self._cancel_job_unlocked(job_id)
                    continue
            response = {
                "job": job,
                "poll_timeout_s": configured_poll_timeout_s,
                "upgrade": upgrade,
            }
            return response

    async def heartbeat(self, token: str) -> WorkerHeartbeatResponse:
        """Refresh worker liveness while a long-running job executes."""
        await self.start()
        with self._state_lock:
            self._load_registry_unlocked()
            worker = self._worker_by_token_unlocked(token)
            worker.status = "online"
            worker.last_seen = _utc()
            self._prune_cancelled_jobs_unlocked()
            return {"accepted": True, "name": worker.name}

    async def submit_result(
        self, token: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Accept one result only from the worker assigned to its job."""
        await self.start()
        job_id = str(payload.get("job_id") or "")
        with self._state_lock:
            self._load_registry_unlocked()
            worker = self._worker_by_token_unlocked(token)
            worker.status = "online"
            worker.last_seen = _utc()
            self._prune_cancelled_jobs_unlocked()
            assigned_machine = self.pending_machines.get(job_id)
            if assigned_machine and assigned_machine != worker.name:
                raise PermissionError("remote job belongs to another worker")
            if job_id in self.cancelled_jobs:
                self.cancelled_jobs.pop(job_id, None)
                return {"accepted": False}
            self.pending_machines.pop(job_id, None)
            future = self.pending.pop(job_id, None)
            if future and not future.done():
                future.set_result(payload)
            return {"accepted": bool(future)}

    async def call(
        self,
        machine: str,
        tool: str,
        args: dict[str, Any],
        timeout_s: int | None = None,
    ) -> dict[str, Any]:
        """Queue one bounded remote tool call and await its assigned result."""
        await self.start()
        settings = self._settings()
        effective_timeout = timeout_s or settings.remote_job_timeout_s
        job_id = "job_" + uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        with self._state_lock:
            self._load_registry_unlocked()
            worker = self.workers.get(machine)
            if not worker:
                raise ValueError(f"unknown remote machine: {machine}")
            if _utc() - worker.last_seen > max(
                2 * settings.remote_poll_timeout_s, 60
            ):
                worker.status = "offline"
                raise RuntimeError(f"remote machine is offline: {machine}")
            max_pending = max(1, int(settings.remote_max_pending_jobs))
            machine_pending = sum(
                1
                for value in self.pending_machines.values()
                if value == machine
            )
            if machine_pending >= max_pending:
                raise RuntimeError(f"remote machine queue is full: {machine}")
            self.pending[job_id] = future
            self.pending_machines[job_id] = machine
            queued_job: RemoteQueuedJob = {
                "id": job_id,
                "tool": tool,
                "args": args,
                "expires_at": _utc() + effective_timeout,
            }
            worker.queue.put_nowait(queued_job)
        try:
            result = await asyncio.wait_for(
                asyncio.shield(future), timeout=effective_timeout
            )
        except TimeoutError as exc:
            with self._state_lock:
                self._cancel_job_unlocked(job_id)
            raise TimeoutError(
                f"remote job timed out: {tool} on {machine}"
            ) from exc
        except asyncio.CancelledError:
            with self._state_lock:
                closed = self._closed
                if not closed:
                    self._cancel_job_unlocked(job_id)
            if closed:
                raise RemoteManagerClosedError(
                    "remote manager is closed"
                ) from None
            raise
        finally:
            with self._state_lock:
                if future.done() and not future.cancelled():
                    self.pending.pop(job_id, None)
                    self.pending_machines.pop(job_id, None)
        if not result.get("ok", False):
            data = result.get("data")
            if not isinstance(data, dict):
                data = {
                    "status": "error",
                    "error_type": result.get("error", "remote_error"),
                    "message": result.get("message", "remote job failed"),
                }
            return _ok(data)
        return _ok(result.get("data"))

    def supports(self, machine: str, capability: str) -> bool:
        """Return whether one known online worker advertises a protocol capability."""
        with self._state_lock:
            self._load_registry_unlocked()
            worker = self.workers.get(machine)
            if worker is None:
                return False
            offline_after_s = max(
                2 * self._settings().remote_poll_timeout_s, 60
            )
            if _utc() - worker.last_seen > offline_after_s:
                return False
            return capability in worker.capabilities

    def list_machines(self) -> RemoteListMachinesOutput:
        """Return synchronized worker inventory and online/offline counts."""
        with self._state_lock:
            self._load_registry_unlocked()
            now = _utc()
            offline_after_s = max(
                2 * self._settings().remote_poll_timeout_s, 60
            )
            rows = []
            counts = {"online": 0, "offline": 0}
            for worker in self.workers.values():
                profile_id, reconnect_command = _worker_reconnect_metadata(
                    worker.info
                )
                last_seen_age_s = (
                    None
                    if not worker.last_seen
                    else max(0.0, now - worker.last_seen)
                )
                status = (
                    "online"
                    if last_seen_age_s is not None
                    and last_seen_age_s <= offline_after_s
                    else "offline"
                )
                worker.status = status
                counts[status] += 1
                rows.append(
                    {
                        "name": worker.name,
                        "status": status,
                        "workdir": worker.workdir,
                        "profile_id": profile_id,
                        "reconnect_command": reconnect_command,
                        "last_seen": worker.last_seen,
                        "last_seen_age_s": last_seen_age_s,
                        "offline_after_s": offline_after_s,
                        "queue_depth": worker.queue.qsize(),
                        "capabilities": worker.capabilities,
                        "info": worker.info,
                    }
                )
            rows.sort(
                key=lambda item: (item["status"] != "online", item["name"])
            )
            return RemoteListMachinesOutput(
                machines=rows, counts={**counts, "total": len(rows)}
            )

    def reconnect_command(self, machine: str) -> RemoteReconnectCommandOutput:
        """Return the credential-free command for one profile-aware worker."""
        with self._state_lock:
            self._load_registry_unlocked()
            worker = self.workers.get(machine)
            if worker is None:
                raise ValueError(f"unknown remote machine: {machine}")
            profile_id, command = _worker_reconnect_metadata(worker.info)
            if profile_id is None or command is None:
                raise ValueError(
                    f"remote machine has no reconnect profile metadata: {machine}"
                )
            return RemoteReconnectCommandOutput(
                machine=worker.name,
                profile_id=profile_id,
                command=command,
            )

    def revoke(self, machine: str) -> RemoteRevokeMachineOutput:
        """Remove one registration and cancel its outstanding jobs."""
        self._require_not_closed()
        with self._state_lock:
            self._load_registry_unlocked()
            worker = self.workers.pop(machine, None)
            if not worker:
                raise ValueError(f"unknown remote machine: {machine}")
            self.tokens.pop(worker.token, None)
            for job_id, pending_machine in list(self.pending_machines.items()):
                if pending_machine == machine:
                    self._cancel_job_unlocked(job_id)
            self._save_registry_unlocked()
            return RemoteRevokeMachineOutput(machine=machine, revoked=True)

    def rename(self, machine: str, new_name: str) -> RemoteRenameMachineOutput:
        """Rename one registered worker and update pending ownership."""
        self._require_not_closed()
        normalized_name = _validate_machine_name(new_name)
        with self._state_lock:
            self._load_registry_unlocked()
            if normalized_name in self.workers:
                raise ValueError(
                    f"machine name already exists: {normalized_name}"
                )
            worker = self.workers.pop(machine, None)
            if not worker:
                raise ValueError(f"unknown remote machine: {machine}")
            worker.name = normalized_name
            self.workers[normalized_name] = worker
            self.tokens[worker.token] = normalized_name
            for job_id, pending_machine in list(self.pending_machines.items()):
                if pending_machine == machine:
                    self.pending_machines[job_id] = normalized_name
            self._save_registry_unlocked()
            return RemoteRenameMachineOutput(
                old_name=machine, new_name=normalized_name
            )

    def _worker_by_token_unlocked(self, token: str) -> RemoteWorker:
        name = self.tokens.get(token)
        if not name:
            raise PermissionError("invalid worker token")
        worker = self.workers.get(name)
        if not worker:
            raise PermissionError("worker token is no longer valid")
        return worker

    def _worker_by_token(self, token: str) -> RemoteWorker:
        with self._state_lock:
            self._load_registry_unlocked()
            return self._worker_by_token_unlocked(token)


_REMOTE_MANAGER: RemoteManager | None = None


def configure_remote_manager(
    manager: RemoteManager | None,
) -> RemoteManager | None:
    """Install a non-owning compatibility binding and return the previous manager."""
    global _REMOTE_MANAGER
    previous = _REMOTE_MANAGER
    _REMOTE_MANAGER = manager
    return previous


def remote_manager() -> RemoteManager:
    """Return the controller-owned manager through the compatibility seam."""
    if _REMOTE_MANAGER is None:
        raise RuntimeError(
            "remote manager is not configured; start ControlRuntime"
        )
    return _REMOTE_MANAGER
