"""Optional S3-compatible byte route for cross-executor session copy."""

import asyncio
import importlib
from collections.abc import Callable
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
)

from ..persistence import StateStore

_OBJECT_STORE_STATE_VERSION = 1
_MAX_CLEANUP_RECORDS = 128
_TRANSFER_ID = Annotated[
    str,
    StringConstraints(pattern=r"^copy_[A-Za-z0-9_-]{22,}$", max_length=128),
]


class ObjectStoreDependencyError(RuntimeError):
    """Raised when object-store transfer is configured without its optional SDK."""


class ObjectStoreRouteUnavailable(RuntimeError):
    """Raised when the optional object-store route can safely fall back."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


class ObjectStoreCleanupRecord(BaseModel):
    """Durable authority for deleting one temporary transfer object."""

    model_config = ConfigDict(strict=True, extra="forbid")

    transfer_id: _TRANSFER_ID
    bucket: str
    key: str


class _ObjectStoreCleanupRegistry(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    version: int = _OBJECT_STORE_STATE_VERSION
    objects: dict[_TRANSFER_ID, ObjectStoreCleanupRecord] = Field(
        default_factory=dict
    )


class S3ObjectTransferService:
    """Presign temporary S3-compatible objects and durably clean them up."""

    def __init__(
        self,
        state_store: StateStore,
        *,
        bucket: str | None,
        prefix: str,
        region: str | None,
        endpoint_url: str | None,
        presign_ttl_s: int,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._state_store = state_store
        self._bucket = str(bucket or "").strip()
        self._prefix = str(prefix or "").strip("/")
        self._region = None if region is None else str(region).strip() or None
        self._endpoint_url = (
            None if endpoint_url is None else str(endpoint_url).strip() or None
        )
        if self._endpoint_url is not None:
            parsed = urlsplit(self._endpoint_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                raise ValueError(
                    "transfer_object_store_endpoint_url must be an absolute "
                    "HTTP(S) URL without credentials or fragment"
                )
        self._presign_ttl_s = max(1, int(presign_ttl_s))
        self._client_factory = client_factory
        self._client_instance: Any | None = None
        self._active: set[str] = set()

    @property
    def enabled(self) -> bool:
        """Return whether bucket configuration enables this route."""
        return bool(self._bucket)

    @property
    def path(self):
        """Return the durable cleanup registry path."""
        return self._state_store.layout.control_transfer_objects_path

    def _load_unlocked(self) -> _ObjectStoreCleanupRegistry:
        raw = self._state_store.read_json(self.path, max_bytes=1024 * 1024)
        if raw is None:
            return _ObjectStoreCleanupRegistry()
        try:
            return _ObjectStoreCleanupRegistry.model_validate(raw)
        except ValidationError:
            raise RuntimeError(
                "object-store transfer cleanup registry is invalid"
            ) from None

    def _save_unlocked(self, registry: _ObjectStoreCleanupRegistry) -> None:
        self._state_store.write_json(
            self.path, registry.model_dump(mode="json")
        )

    def _client(self) -> Any:
        if self._client_instance is not None:
            return self._client_instance
        if self._client_factory is not None:
            client = self._client_factory()
        else:
            try:
                boto3 = importlib.import_module("boto3")
            except ImportError as exc:
                raise ObjectStoreDependencyError(
                    "object-store transfer requires optional dependency "
                    "'workgate[s3]'"
                ) from exc
            client = boto3.client(
                "s3",
                region_name=self._region,
                endpoint_url=self._endpoint_url,
            )
        self._client_instance = client
        return client

    def _key(self, transfer_id: str) -> str:
        parts = [
            part for part in (self._prefix, "transfers", transfer_id) if part
        ]
        return "/".join(parts)

    def begin_attempt(
        self, transfer_id: str
    ) -> tuple[ObjectStoreCleanupRecord, str]:
        """Persist cleanup authority and return one short-lived PUT URL."""
        if not self.enabled:
            raise RuntimeError("object-store transfer is not configured")
        client = self._client()
        record = ObjectStoreCleanupRecord(
            transfer_id=str(transfer_id),
            bucket=self._bucket,
            key=self._key(str(transfer_id)),
        )
        with self._state_store.transaction(self.path):
            registry = self._load_unlocked()
            existing = registry.objects.get(record.transfer_id)
            inactive = set(registry.objects) - self._active
            if inactive:
                raise ObjectStoreRouteUnavailable(
                    "setup",
                    "object-store cleanup is pending from an earlier transfer",
                )
            if (
                existing is None
                and len(registry.objects) >= _MAX_CLEANUP_RECORDS
            ):
                raise ObjectStoreRouteUnavailable(
                    "setup",
                    "object-store cleanup registry is full",
                )
            if existing is not None and (
                existing.bucket != record.bucket or existing.key != record.key
            ):
                raise RuntimeError(
                    "object-store transfer cleanup contract changed"
                )
            put_url = str(
                client.generate_presigned_url(
                    "put_object",
                    Params={"Bucket": record.bucket, "Key": record.key},
                    ExpiresIn=self._presign_ttl_s,
                    HttpMethod="PUT",
                )
            )
            registry.objects[record.transfer_id] = record
            self._save_unlocked(registry)
        self._active.add(record.transfer_id)
        return record, put_url

    def presign_get(self, attempt: ObjectStoreCleanupRecord) -> str:
        """Return one short-lived GET URL without exposing storage credentials."""
        return str(
            self._client().generate_presigned_url(
                "get_object",
                Params={"Bucket": attempt.bucket, "Key": attempt.key},
                ExpiresIn=self._presign_ttl_s,
                HttpMethod="GET",
            )
        )

    def _remove_record(self, transfer_id: str) -> None:
        with self._state_store.transaction(self.path):
            registry = self._load_unlocked()
            if registry.objects.pop(transfer_id, None) is None:
                return
            self._save_unlocked(registry)

    async def _delete(self, record: ObjectStoreCleanupRecord) -> str | None:
        try:
            await asyncio.to_thread(
                self._client().delete_object,
                Bucket=record.bucket,
                Key=record.key,
            )
        except Exception as exc:
            return type(exc).__name__
        return None

    async def finish_attempt(
        self, attempt: ObjectStoreCleanupRecord
    ) -> str | None:
        """Best-effort delete one attempted object and release live ownership."""
        try:
            cleanup_error = await self._delete(attempt)
            if cleanup_error is not None:
                return cleanup_error
            try:
                self._remove_record(attempt.transfer_id)
            except Exception as exc:
                return type(exc).__name__
            return None
        finally:
            self._active.discard(attempt.transfer_id)

    async def reconcile_orphans(self) -> tuple[str, ...]:
        """Retry deletion for persisted objects not owned by a live attempt."""
        with self._state_store.transaction(self.path):
            registry = self._load_unlocked()
        active = set(self._active)
        pending = [
            transfer_id
            for transfer_id in registry.objects
            if transfer_id not in active
        ]
        if pending and not self.enabled:
            return (
                "object-store cleanup is pending while the route is disabled",
            )
        errors: list[str] = []
        for transfer_id, record in tuple(registry.objects.items()):
            if transfer_id in active:
                continue
            cleanup_error = await self._delete(record)
            if cleanup_error is None:
                try:
                    self._remove_record(transfer_id)
                except Exception as exc:
                    cleanup_error = type(exc).__name__
            if cleanup_error is not None:
                errors.append(f"object-store cleanup failed ({cleanup_error})")
        return tuple(errors)
