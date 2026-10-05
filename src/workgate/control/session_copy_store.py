"""Feature-specific durable checkpoints for cross-executor session copy."""

import time
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from ..jobs.persistence import JOB_STORE_LEGACY_VERSIONS, JOB_STORE_VERSION
from ..persistence import StateStore
from ..protocol.ids import ExecutorId, PayloadId, SessionId
from .payload_store import PayloadStore

CHECKPOINT_STORE_VERSION = 1
UNMANAGED_CHECKPOINT_STALE_S = 24 * 60 * 60
_CopyTransferId = Annotated[
    str,
    StringConstraints(pattern=r"^copy_[A-Za-z0-9_-]{22,}$", max_length=128),
]
_JobId = Annotated[
    str,
    StringConstraints(pattern=r"^job_[A-Za-z0-9_-]{12,}$", max_length=128),
]
_Sha256 = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64),
]


class SessionCopyCheckpoint(BaseModel):
    """Restart-critical facts for one cross-executor transfer only."""

    model_config = ConfigDict(strict=True, extra="forbid")

    transfer_id: _CopyTransferId
    owner_job_id: _JobId | None = None
    source_session_id: SessionId
    source_executor_id: ExecutorId
    destination_session_id: SessionId
    destination_executor_id: ExecutorId
    source_path: str
    destination_path: str
    kind: Literal["file", "dir"]
    overwrite: bool
    chunk_size: Annotated[int, Field(gt=0)]
    payload_id: PayloadId
    payload_size: Annotated[int, Field(ge=0)]
    payload_sha256: _Sha256
    source_resolved_path: str
    import_path: str | None = None
    import_resource_id: str | None = None
    destination_resolved_path: str | None = None
    entries: Annotated[int, Field(ge=0)] | None = None
    chunks: Annotated[int, Field(ge=0)] = 0
    resumed_bytes: Annotated[int, Field(ge=0)] = 0
    cleanup_errors: list[str] = Field(default_factory=list)
    receipts_released: bool = False
    abandoning: bool = False
    payload_retained: bool = True
    last_known_step: Literal["exported", "importing", "imported"] = "exported"
    created_at: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    updated_at: Annotated[float, Field(ge=0, allow_inf_nan=False)]

    @model_validator(mode="after")
    def validate_step(self) -> SessionCopyCheckpoint:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.abandoning != (not self.payload_retained):
            raise ValueError(
                "abandoning checkpoint must release its control payload"
            )
        if self.last_known_step in {"importing", "imported"} and (
            self.import_path is None or self.import_resource_id is None
        ):
            raise ValueError("import checkpoint is missing import identity")
        if self.last_known_step == "imported":
            if self.destination_resolved_path is None:
                raise ValueError(
                    "imported checkpoint is missing destination path"
                )
            if self.kind == "dir" and self.entries is None:
                raise ValueError(
                    "imported directory checkpoint is missing entry count"
                )
        elif self.receipts_released:
            raise ValueError("receipt cleanup can only complete after import")
        return self


class SessionCopyPayloadReservation(BaseModel):
    """Durable byte-capacity reservation held before export payload commit."""

    model_config = ConfigDict(strict=True, extra="forbid")

    transfer_id: _CopyTransferId
    owner_job_id: _JobId | None = None
    payload_size: Annotated[int, Field(ge=0)]
    created_at: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    updated_at: Annotated[float, Field(ge=0, allow_inf_nan=False)]

    @model_validator(mode="after")
    def validate_times(self) -> SessionCopyPayloadReservation:
        if self.updated_at < self.created_at:
            raise ValueError("reservation updated_at cannot precede created_at")
        return self


class TransferPayloadCapacityError(RuntimeError):
    """Raised when durable transfer-payload capacity cannot admit an export."""


class _CheckpointRegistry(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    version: Literal[1]
    transfers: dict[_CopyTransferId, SessionCopyCheckpoint]
    reservations: dict[_CopyTransferId, SessionCopyPayloadReservation] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_reservation_keys(self) -> _CheckpointRegistry:
        if any(
            transfer_id != reservation.transfer_id
            for transfer_id, reservation in self.reservations.items()
        ):
            raise ValueError(
                "session-copy reservation identity does not match its key"
            )
        if set(self.transfers).intersection(self.reservations):
            raise ValueError(
                "session-copy transfer cannot be both reserved and committed"
            )
        return self

    @model_validator(mode="after")
    def validate_transfer_keys(self) -> _CheckpointRegistry:
        if any(
            transfer_id != checkpoint.transfer_id
            for transfer_id, checkpoint in self.transfers.items()
        ):
            raise ValueError(
                "session-copy transfer identity does not match its key"
            )
        return self


class SessionCopyCheckpointStore:
    """Small synchronous repository for session-copy transfer checkpoints."""

    def __init__(self, state_store: StateStore, payloads: PayloadStore) -> None:
        self._state_store = state_store
        self._payloads = payloads

    @property
    def path(self) -> Path:
        return (
            self._state_store.layout.control_dir / "session-copy-transfers.json"
        )

    def _load_registry_unlocked(self) -> _CheckpointRegistry:
        raw = self._state_store.read_json(self.path, max_bytes=2 * 1024 * 1024)
        if raw is None:
            return _CheckpointRegistry(
                version=CHECKPOINT_STORE_VERSION,
                transfers={},
                reservations={},
            )
        try:
            return _CheckpointRegistry.model_validate(raw)
        except ValidationError:
            raise RuntimeError(
                "session-copy checkpoint store is invalid"
            ) from None

    def _load_unlocked(self) -> dict[str, SessionCopyCheckpoint]:
        return dict(self._load_registry_unlocked().transfers)

    def _save_registry_unlocked(self, registry: _CheckpointRegistry) -> None:
        self._state_store.write_json(
            self.path, registry.model_dump(mode="json")
        )

    def load(self, transfer_id: str) -> SessionCopyCheckpoint | None:
        with self._state_store.transaction(self.path):
            return self._load_unlocked().get(transfer_id)

    def load_for_owner_job(
        self, owner_job_id: str
    ) -> SessionCopyCheckpoint | None:
        """Return the sole transfer checkpoint owned by one managed job."""
        with self._state_store.transaction(self.path):
            matches = [
                checkpoint
                for checkpoint in self._load_unlocked().values()
                if checkpoint.owner_job_id == owner_job_id
            ]
        if len(matches) > 1:
            raise RuntimeError(
                f"managed session-copy job owns multiple checkpoints: {owner_job_id}"
            )
        return matches[0] if matches else None

    def _authoritative_job_statuses(self) -> dict[str, str] | None:
        """Return validated primary job authority, or None when it is uncertain."""
        try:
            raw = self._state_store.read_json(
                self._state_store.layout.jobs_store_path,
                max_bytes=8 * 1024 * 1024,
            )
        except OSError, ValueError:
            return None
        if not isinstance(raw, dict):
            return None
        version = raw.get("version")
        if (
            version != JOB_STORE_VERSION
            and version not in JOB_STORE_LEGACY_VERSIONS
        ):
            return None
        rows = raw.get("jobs")
        if not isinstance(rows, list):
            return None
        statuses: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                return None
            job_id = row.get("job_id")
            status = row.get("status")
            if (
                not isinstance(job_id, str)
                or not job_id
                or not isinstance(status, str)
                or not status
            ):
                return None
            statuses[job_id] = status
        return statuses

    def reserve_export(
        self,
        *,
        transfer_id: str,
        owner_job_id: str | None,
        payload_size: int,
        max_payload_bytes: int,
        max_store_bytes: int,
    ) -> SessionCopyPayloadReservation:
        """Durably reserve retained payload capacity before upload bytes arrive."""
        requested = int(payload_size)
        if requested < 0:
            raise ValueError("payload_size must be >= 0")
        if requested > int(max_payload_bytes):
            raise TransferPayloadCapacityError(
                "transfer payload exceeds the per-transfer retained byte limit"
            )
        now = time.time()
        with self._state_store.transaction(self.path):
            registry = self._load_registry_unlocked()
            existing_checkpoint = registry.transfers.get(transfer_id)
            if existing_checkpoint is not None:
                raise RuntimeError(
                    f"session-copy transfer is already committed: {transfer_id}"
                )
            existing = registry.reservations.get(transfer_id)
            if existing is not None:
                if (
                    existing.payload_size != requested
                    or existing.owner_job_id != owner_job_id
                ):
                    raise RuntimeError(
                        "session-copy export reservation contract changed"
                    )
                return existing
            retained = sum(
                checkpoint.payload_size
                for checkpoint in registry.transfers.values()
                if checkpoint.payload_retained
            )
            reserved = sum(
                reservation.payload_size
                for reservation in registry.reservations.values()
            )
            if retained + reserved + requested > int(max_store_bytes):
                raise TransferPayloadCapacityError(
                    "transfer payload store capacity is exhausted"
                )
            reservation = SessionCopyPayloadReservation(
                transfer_id=transfer_id,
                owner_job_id=owner_job_id,
                payload_size=requested,
                created_at=now,
                updated_at=now,
            )
            registry.reservations[transfer_id] = reservation
            self._save_registry_unlocked(registry)
            return reservation

    def commit_export(
        self,
        *,
        checkpoint: dict[str, Any],
        staging_path: Path,
        payload_size: int,
        payload_sha256: str,
    ) -> SessionCopyCheckpoint:
        """Commit payload bytes and their first durable checkpoint under one feature lock."""
        transfer_id = str(checkpoint["transfer_id"])
        with self._state_store.transaction(self.path):
            registry = self._load_registry_unlocked()
            existing = registry.transfers.get(transfer_id)
            if existing is not None:
                return existing
            reservation = registry.reservations.get(transfer_id)
            if reservation is None:
                raise RuntimeError(
                    f"session-copy export reservation is missing: {transfer_id}"
                )
            if reservation.payload_size != int(payload_size):
                raise RuntimeError(
                    "session-copy export reservation size changed before commit"
                )
            if reservation.owner_job_id != checkpoint.get("owner_job_id"):
                raise RuntimeError(
                    "session-copy export reservation owner changed before commit"
                )
            payload = self._payloads.commit_staging(
                staging_path,
                namespace="transfer",
                size=payload_size,
                sha256=payload_sha256,
            )
            now = time.time()
            record = SessionCopyCheckpoint.model_validate(
                {
                    **checkpoint,
                    "payload_id": payload.payload_id,
                    "payload_size": payload.size,
                    "payload_sha256": payload.sha256,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            registry.reservations.pop(transfer_id, None)
            registry.transfers[transfer_id] = record
            self._save_registry_unlocked(registry)
            return record

    def update(self, transfer_id: str, **changes: Any) -> SessionCopyCheckpoint:
        with self._state_store.transaction(self.path):
            registry = self._load_registry_unlocked()
            current = registry.transfers.get(transfer_id)
            if current is None:
                raise RuntimeError(
                    f"session-copy checkpoint is missing: {transfer_id}"
                )
            updated = current.model_copy(
                update={**changes, "updated_at": time.time()}
            )
            updated = SessionCopyCheckpoint.model_validate(updated.model_dump())
            registry.transfers[transfer_id] = updated
            self._save_registry_unlocked(registry)
            return updated

    def remove(self, transfer_id: str) -> None:
        """Persist checkpoint removal before deleting its immutable payload."""
        payload_id: str | None = None
        with self._state_store.transaction(self.path):
            registry = self._load_registry_unlocked()
            removed = registry.transfers.pop(transfer_id, None)
            if removed is None:
                return
            payload_id = str(removed.payload_id)
            self._save_registry_unlocked(registry)
        if payload_id is not None:
            self._payloads.remove_payload(payload_id, namespace="transfer")

    def prepare_abandonment(
        self, transfer_id: str
    ) -> SessionCopyCheckpoint | None:
        """Durably release reservation or retain a tombstone before payload cleanup."""
        with self._state_store.transaction(self.path):
            registry = self._load_registry_unlocked()
            checkpoint = registry.transfers.get(transfer_id)
            if checkpoint is None:
                if registry.reservations.pop(transfer_id, None) is not None:
                    self._save_registry_unlocked(registry)
                return None
            if not checkpoint.abandoning or checkpoint.payload_retained:
                checkpoint = SessionCopyCheckpoint.model_validate(
                    checkpoint.model_copy(
                        update={
                            "abandoning": True,
                            "payload_retained": False,
                            "updated_at": time.time(),
                        }
                    ).model_dump()
                )
                registry.transfers[transfer_id] = checkpoint
                self._save_registry_unlocked(registry)
            referenced = {
                str(item.payload_id)
                for item in registry.transfers.values()
                if item.payload_retained
            }
            self._payloads.prune_unreferenced_payloads("transfer", referenced)
            return checkpoint

    def prepare_abandonments(
        self, *, executor_id: str | None = None
    ) -> tuple[SessionCopyCheckpoint, ...]:
        """Release unreachable reservations/payloads under one feature lock."""
        now = time.time()
        jobs = self._authoritative_job_statuses()
        with self._state_store.transaction(self.path):
            registry = self._load_registry_unlocked()
            transfers = registry.transfers
            changed = False

            for transfer_id, reservation in tuple(
                registry.reservations.items()
            ):
                owner = reservation.owner_job_id
                should_release = False
                if owner is None:
                    should_release = (
                        now - reservation.updated_at
                        >= UNMANAGED_CHECKPOINT_STALE_S
                    )
                elif jobs is not None:
                    status = jobs.get(owner)
                    should_release = status is None or status == "succeeded"
                if should_release:
                    registry.reservations.pop(transfer_id, None)
                    changed = True

            candidates: list[SessionCopyCheckpoint] = []
            for transfer_id, checkpoint in transfers.items():
                owner = checkpoint.owner_job_id
                should_abandon = checkpoint.abandoning
                if not should_abandon and owner is None:
                    should_abandon = (
                        now - checkpoint.updated_at
                        >= UNMANAGED_CHECKPOINT_STALE_S
                    )
                elif (
                    not should_abandon
                    and owner is not None
                    and jobs is not None
                ):
                    status = jobs.get(owner)
                    should_abandon = status is None or status == "succeeded"
                if not should_abandon:
                    continue
                if not checkpoint.abandoning or checkpoint.payload_retained:
                    checkpoint = SessionCopyCheckpoint.model_validate(
                        checkpoint.model_copy(
                            update={
                                "abandoning": True,
                                "payload_retained": False,
                                "updated_at": now,
                            }
                        ).model_dump()
                    )
                    transfers[transfer_id] = checkpoint
                    changed = True
                if (
                    executor_id is None
                    or str(checkpoint.destination_executor_id) == executor_id
                ):
                    candidates.append(checkpoint)
            if changed:
                self._save_registry_unlocked(registry)
            referenced = {
                str(checkpoint.payload_id)
                for checkpoint in transfers.values()
                if checkpoint.payload_retained
            }
            # Keep payload GC under the same feature lock as checkpoint mutation.
            # Persisting abandoning=true first means a crash cannot delete the
            # last recovery authority before dropping a large control payload.
            self._payloads.prune_unreferenced_payloads("transfer", referenced)
            self._payloads.prune_staging_files("transfer")
            return tuple(candidates)
