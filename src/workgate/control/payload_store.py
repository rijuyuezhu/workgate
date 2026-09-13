"""Control-owned immutable payload bytes shared by feature-specific resources."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from ..app_paths import ensure_private_directory
from ..protocol.ids import new_payload_id

PAYLOAD_SUFFIX = ".bin"
STAGING_SUFFIX = ".tmp"
STAGING_PRUNE_GRACE_S = 3600
_PAYLOAD_ID_RE = re.compile(r"^payload_[A-Za-z0-9_-]{22,}$")
_PAYLOAD_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class PayloadDescriptor:
    """Durable identity and integrity metadata for one immutable payload."""

    payload_id: str
    size: int
    sha256: str


@dataclass(frozen=True)
class PayloadStore:
    """Private immutable payload storage rooted in control-owned persistent data."""

    data_dir: Path

    def _namespace(self, namespace: str) -> str:
        value = str(namespace)
        if not _PAYLOAD_NAMESPACE_RE.fullmatch(value):
            raise ValueError("invalid payload namespace")
        return value

    def root_directory(self) -> Path:
        """Return the private root containing all feature-owned payload namespaces."""
        data_root = ensure_private_directory(self.data_dir)
        control_root = ensure_private_directory(data_root / "control")
        return ensure_private_directory(control_root / "payloads")

    def directory(self, namespace: str) -> Path:
        """Return one feature-owned private payload namespace."""
        return ensure_private_directory(
            self.root_directory() / self._namespace(namespace)
        )

    def path(self, payload_id: str, *, namespace: str) -> Path:
        """Resolve one validated payload id inside a feature-owned namespace."""
        value = str(payload_id)
        if not _PAYLOAD_ID_RE.fullmatch(value):
            raise ValueError("invalid payload id")
        return self.directory(namespace) / f"{value}{PAYLOAD_SUFFIX}"

    def new_staging_path(self, namespace: str) -> Path:
        """Allocate one staging path inside a feature-owned payload namespace."""
        return (
            self.directory(namespace) / f".{uuid.uuid4().hex}{STAGING_SUFFIX}"
        )

    def open_private_staging(self, path: Path, *, namespace: str) -> BinaryIO:
        """Open a new staging file without following or replacing an existing leaf."""
        directory = self.directory(namespace)
        if path.parent != directory or path.name != Path(path.name).name:
            raise ValueError(
                "payload staging path is outside the payload directory"
            )
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return os.fdopen(descriptor, "wb")

    def commit_staging(
        self,
        staging_path: Path,
        *,
        namespace: str,
        size: int,
        sha256: str,
        payload_id: str | None = None,
    ) -> PayloadDescriptor:
        """Atomically publish already-verified staging bytes under an opaque id."""
        directory = self.directory(namespace)
        if staging_path.parent != directory or not staging_path.name.endswith(
            STAGING_SUFFIX
        ):
            raise ValueError(
                "payload staging path is outside the payload directory"
            )
        requested_id = payload_id or str(new_payload_id())
        destination = self.path(requested_id, namespace=namespace)
        if destination.exists():
            raise FileExistsError(f"payload already exists: {requested_id}")
        info = staging_path.stat()
        if not stat.S_ISREG(info.st_mode) or int(info.st_size) != int(size):
            raise ValueError("payload staging size changed before commit")
        os.replace(staging_path, destination)
        with contextlib.suppress(OSError):
            destination.chmod(0o600)
        return PayloadDescriptor(
            payload_id=requested_id,
            size=int(size),
            sha256=str(sha256),
        )

    def open_payload(
        self,
        payload_id: str,
        *,
        namespace: str,
        size: int,
        sha256: str,
    ) -> tuple[BinaryIO, Path]:
        """Open one immutable payload and verify its stored size and digest."""
        path = self.path(payload_id, namespace=namespace)
        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        handle: BinaryIO | None = None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("control payload is not a regular file")
            if int(info.st_size) != int(size):
                raise ValueError("control payload size changed")
            handle = os.fdopen(descriptor, "rb")
            digest = hashlib.sha256()
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
            if digest.hexdigest() != str(sha256):
                raise ValueError("control payload digest changed")
            handle.seek(0)
            return handle, path
        except Exception:
            if handle is not None:
                handle.close()
            else:
                os.close(descriptor)
            raise

    def remove_payload(self, payload_id: str, *, namespace: str) -> None:
        """Best-effort remove one immutable payload by management identity."""
        try:
            path = self.path(payload_id, namespace=namespace)
        except ValueError:
            return
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)

    def prune_staging_files(self, namespace: str) -> bool:
        """Remove stale uncommitted staging files owned by one feature namespace."""
        directory = self.directory(namespace)
        changed = False
        prune_before = time.time() - STAGING_PRUNE_GRACE_S
        for path in directory.glob(f".*{STAGING_SUFFIX}"):
            try:
                if path.stat().st_mtime > prune_before:
                    continue
                path.unlink(missing_ok=True)
            except OSError:
                continue
            changed = True
        return changed

    def prune_unreferenced_payloads(
        self, namespace: str, referenced_payload_ids: set[str]
    ) -> bool:
        """Remove committed payloads unreferenced by one feature's durable registry."""
        directory = self.directory(namespace)
        changed = False
        for path in directory.glob(f"*{PAYLOAD_SUFFIX}"):
            payload_id = path.name[: -len(PAYLOAD_SUFFIX)]
            if payload_id in referenced_payload_ids:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            changed = True
        return changed
