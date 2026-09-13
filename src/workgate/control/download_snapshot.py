"""Control-owned private immutable snapshots used by public file links."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from ..config.settings import get_settings
from .payload_store import PAYLOAD_SUFFIX, PayloadStore

SNAPSHOT_SUFFIX = PAYLOAD_SUFFIX


@dataclass(frozen=True)
class DownloadSnapshot:
    """One creation-time file snapshot awaiting durable registration."""

    staging_path: Path
    display_path: str
    source_name: str
    size: int
    sha256: str


def _payload_store(data_dir: Path | None = None) -> PayloadStore:
    """Return the download payload store, with ambient settings only as compatibility fallback."""
    root = get_settings().data_dir if data_dir is None else data_dir
    return PayloadStore(root)


def snapshot_directory(*, data_dir: Path | None = None) -> Path:
    """Return the feature-owned immutable download payload directory."""
    return _payload_store(data_dir).directory("download")


def new_staging_path(*, data_dir: Path | None = None) -> Path:
    """Allocate a private staging path in the download payload directory."""
    return _payload_store(data_dir).new_staging_path("download")


def open_private_staging(
    path: Path, *, data_dir: Path | None = None
) -> BinaryIO:
    """Create and open one exclusive private staging file."""
    return _payload_store(data_dir).open_private_staging(
        path, namespace="download"
    )


def assert_shareable_size(size: int, *, maximum: int | None = None) -> None:
    """Reject invalid or over-limit public snapshot sizes."""
    limit = (
        get_settings().file_download_max_file_bytes
        if maximum is None
        else int(maximum)
    )
    if size < 0:
        raise ValueError("File size is invalid")
    if limit > 0 and size > limit:
        raise ValueError(f"File is too large: {size}")
