"""Control-owned private immutable snapshots used by public file links."""

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

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


def _payload_store(data_dir: Path) -> PayloadStore:
    return PayloadStore(data_dir)


def snapshot_directory(*, data_dir: Path) -> Path:
    """Return the feature-owned immutable download payload directory."""
    return _payload_store(data_dir).directory("download")


def new_staging_path(*, data_dir: Path) -> Path:
    """Allocate a private staging path in the download payload directory."""
    return _payload_store(data_dir).new_staging_path("download")


def open_private_staging(path: Path, *, data_dir: Path) -> BinaryIO:
    """Create and open one exclusive private staging file."""
    return _payload_store(data_dir).open_private_staging(
        path, namespace="download"
    )


def assert_shareable_size(size: int, *, maximum: int) -> None:
    """Reject invalid or over-limit public snapshot sizes."""
    limit = int(maximum)
    if size < 0:
        raise ValueError("File size is invalid")
    if limit > 0 and size > limit:
        raise ValueError(f"File is too large: {size}")
