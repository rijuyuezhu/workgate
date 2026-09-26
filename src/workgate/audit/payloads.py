"""Private content-addressed storage for sanitized audit payload values."""

import contextlib
import gzip
import hashlib
import io
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any

from ..config.role_config import SharedRoleConfig
from ..persistence import StateLayout
from ..utils.private_files import atomic_write_private_bytes

AUDIT_PAYLOAD_KEY = "$workgate_audit_payload"
AUDIT_PAYLOAD_VERSION = 1
_AUDIT_TRUNCATED_KEY = "$workgate_audit_truncated"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode sanitized JSON data canonically for stable content addressing."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _deterministic_gzip(data: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", fileobj=buffer, compresslevel=9, mtime=0
    ) as handle:
        handle.write(data)
    return buffer.getvalue()


def _payload_path(settings: SharedRoleConfig, digest: str) -> Path:
    if not _DIGEST_RE.fullmatch(digest):
        raise ValueError("invalid audit payload digest")
    return (
        StateLayout(settings.state_dir).audit_payload_dir / f"{digest}.json.gz"
    )


def _prepare_payload_root(settings: SharedRoleConfig) -> Path:
    root = StateLayout(settings.state_dir).audit_payload_dir
    if root.is_symlink():
        raise OSError("audit payload directory is a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise OSError("audit payload directory is not a regular directory")
    if os.name != "nt":
        os.chmod(root, 0o700)
    return root


def _reference_metadata(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {AUDIT_PAYLOAD_KEY}:
        return None
    metadata = value.get(AUDIT_PAYLOAD_KEY)
    if not isinstance(metadata, dict):
        return None
    if metadata.get("version") != AUDIT_PAYLOAD_VERSION:
        return None
    digest = str(metadata.get("sha256") or "")
    if not _DIGEST_RE.fullmatch(digest):
        return None
    return metadata


def payload_reference_metadata(value: Any) -> list[dict[str, Any]]:
    """Collect valid reference metadata objects from a JSON-compatible value."""
    metadata = _reference_metadata(value)
    if metadata is not None:
        return [metadata]
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for child in value.values():
            found.extend(payload_reference_metadata(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(payload_reference_metadata(child))
    return found


def payload_reference_digests(value: Any) -> set[str]:
    """Collect valid payload digests from a JSON-compatible value."""
    metadata = _reference_metadata(value)
    if metadata is not None:
        return {str(metadata["sha256"])}
    found: set[str] = set()
    if isinstance(value, dict):
        for child in value.values():
            found.update(payload_reference_digests(child))
    elif isinstance(value, list):
        for child in value:
            found.update(payload_reference_digests(child))
    return found


def externalize_sanitized_value(
    value: Any,
    *,
    settings: SharedRoleConfig,
    preview: Any,
    created_at: float,
) -> Any:
    """Keep one sanitized value inline or replace it with a recoverable reference."""
    encoded = canonical_json_bytes(value)
    if (
        not settings.audit_payloads_enabled
        or len(encoded) <= settings.audit_inline_value_bytes
    ):
        return value
    if len(encoded) > settings.max_audit_payload_bytes:
        return {
            _AUDIT_TRUNCATED_KEY: {
                "reason": "payload_byte_limit",
                "original_bytes": len(encoded),
                "max_bytes": settings.max_audit_payload_bytes,
                "preview": preview,
            }
        }

    compressed = _deterministic_gzip(encoded)
    if len(compressed) > settings.max_audit_payload_store_bytes:
        return {
            _AUDIT_TRUNCATED_KEY: {
                "reason": "payload_store_object_limit",
                "original_bytes": len(encoded),
                "compressed_bytes": len(compressed),
                "max_store_bytes": settings.max_audit_payload_store_bytes,
                "preview": preview,
            }
        }
    digest = hashlib.sha256(encoded).hexdigest()
    path = _payload_path(settings, digest)
    try:
        _prepare_payload_root(settings)
        if path.exists() and not path.is_symlink() and not path.is_file():
            raise OSError("audit payload path is not a regular file")
        atomic_write_private_bytes(path, compressed)
    except OSError:
        return {
            _AUDIT_TRUNCATED_KEY: {
                "reason": "payload_store_error",
                "original_bytes": len(encoded),
                "preview": preview,
            }
        }
    return {
        AUDIT_PAYLOAD_KEY: {
            "version": AUDIT_PAYLOAD_VERSION,
            "sha256": digest,
            "json_bytes": len(encoded),
            "created_at": float(created_at),
            "preview": preview,
        }
    }


def _read_regular_file_nofollow(path: Path, max_bytes: int) -> bytes:
    if path.is_symlink():
        raise ValueError("audit payload path is a symlink")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("audit payload path is not a regular file")
        if info.st_size > max_bytes:
            raise ValueError("compressed audit payload exceeds read bound")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("compressed audit payload exceeds read bound")
        return data
    finally:
        os.close(descriptor)


def _unresolved_reference(
    metadata: dict[str, Any], status: str
) -> dict[str, Any]:
    return {AUDIT_PAYLOAD_KEY: {**metadata, "status": status}}


def resolve_payload_reference(value: Any, settings: SharedRoleConfig) -> Any:
    """Resolve one reference after validating retention, compression, size, and digest."""
    metadata = _reference_metadata(value)
    if metadata is None:
        return value
    created_at = metadata.get("created_at")
    try:
        if created_at is None:
            raise ValueError("missing created_at")
        age = max(0.0, time.time() - float(created_at))
    except TypeError, ValueError:
        return _unresolved_reference(metadata, "corrupt")
    if (
        settings.audit_payload_retention_s == 0
        or age > settings.audit_payload_retention_s
    ):
        return _unresolved_reference(metadata, "expired")

    digest = str(metadata["sha256"])
    try:
        json_bytes = metadata.get("json_bytes")
        if json_bytes is None:
            raise ValueError("missing json_bytes")
        expected_bytes = int(json_bytes)
    except TypeError, ValueError:
        return _unresolved_reference(metadata, "corrupt")
    if not 0 <= expected_bytes <= settings.max_audit_payload_bytes:
        return _unresolved_reference(metadata, "corrupt")
    path = _payload_path(settings, digest)
    if not path.exists():
        return _unresolved_reference(metadata, "missing")
    compressed_bound = settings.max_audit_payload_bytes + 1_048_576
    try:
        compressed = _read_regular_file_nofollow(path, compressed_bound)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as handle:
            decoded = handle.read(settings.max_audit_payload_bytes + 1)
            if len(decoded) > settings.max_audit_payload_bytes:
                raise ValueError("audit payload decompression limit exceeded")
            if handle.read(1):
                raise ValueError("audit payload decompression limit exceeded")
        if len(decoded) != expected_bytes:
            raise ValueError("audit payload byte count mismatch")
        if hashlib.sha256(decoded).hexdigest() != digest:
            raise ValueError("audit payload digest mismatch")
        return json.loads(decoded)
    except FileNotFoundError:
        return _unresolved_reference(metadata, "missing")
    except (
        OSError,
        EOFError,
        ValueError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return _unresolved_reference(metadata, "corrupt")


def resolve_payload_references(value: Any, settings: SharedRoleConfig) -> Any:
    """Resolve references in one coalesced audit result without reinterpreting payload data."""
    metadata = _reference_metadata(value)
    if metadata is not None:
        return resolve_payload_reference(value, settings)
    if isinstance(value, dict):
        return {
            key: resolve_payload_references(child, settings)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [resolve_payload_references(child, settings) for child in value]
    return value


def payload_files(settings: SharedRoleConfig) -> list[Path]:
    """List bounded-store payload candidates without following directories or symlinks."""
    root = StateLayout(settings.state_dir).audit_payload_dir
    if not root.exists() or root.is_symlink() or not root.is_dir():
        return []
    return list(root.glob("*.json.gz"))


def payload_file_sizes(settings: SharedRoleConfig) -> dict[str, int]:
    """Return regular content-addressed payload sizes keyed by digest."""
    sizes: dict[str, int] = {}
    for path in payload_files(settings):
        digest = path.name.removesuffix(".json.gz")
        if not _DIGEST_RE.fullmatch(digest) or path.is_symlink():
            continue
        with contextlib.suppress(OSError):
            info = path.stat()
            if stat.S_ISREG(info.st_mode):
                sizes[digest] = info.st_size
    return sizes


def prune_payload_files(
    settings: SharedRoleConfig,
    *,
    referenced: set[str],
    active_references: set[str],
) -> None:
    """Remove expired/orphan/corrupt-name objects and enforce the compressed store quota."""
    now = time.time()
    candidates: list[tuple[float, int, str, Path]] = []
    for path in payload_files(settings):
        digest = path.name.removesuffix(".json.gz")
        try:
            info = path.lstat()
        except OSError:
            continue
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or not _DIGEST_RE.fullmatch(digest)
        ):
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        age = max(0.0, now - info.st_mtime)
        if digest not in referenced and (
            settings.audit_payload_retention_s == 0
            or age > settings.audit_payload_retention_s
        ):
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        if digest in referenced and digest not in active_references:
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        candidates.append((info.st_mtime, info.st_size, digest, path))

    total = sum(size for _, size, _, _ in candidates)
    if total <= settings.max_audit_payload_store_bytes:
        return
    candidates.sort(key=lambda item: (item[2] in active_references, item[0]))
    for _, size, _, path in candidates:
        if total <= settings.max_audit_payload_store_bytes:
            break
        with contextlib.suppress(OSError):
            path.unlink()
            total -= size
