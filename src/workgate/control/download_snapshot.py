"""Control-owned private immutable snapshots used by public file links."""

from __future__ import annotations

import contextlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from ..config.settings import get_settings
from ..persistence import get_state_store

SNAPSHOT_SUFFIX = ".bin"
STAGING_SUFFIX = ".tmp"


@dataclass(frozen=True)
class DownloadSnapshot:
    """One creation-time file snapshot awaiting durable registration."""

    staging_path: Path
    display_path: str
    source_name: str
    size: int
    sha256: str


def snapshot_directory() -> Path:
    """Return the private control snapshot directory."""
    path = get_state_store().layout.download_snapshots_dir
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return path


def new_staging_path() -> Path:
    """Allocate a private staging path in the snapshot directory."""
    return snapshot_directory() / f".{uuid.uuid4().hex}{STAGING_SUFFIX}"


def new_snapshot_path() -> Path:
    """Allocate an opaque final snapshot path."""
    return snapshot_directory() / f"{uuid.uuid4().hex}{SNAPSHOT_SUFFIX}"


def open_private_staging(path: Path) -> BinaryIO:
    """Create and open one exclusive private staging file."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "wb")


def assert_shareable_size(size: int) -> None:
    """Reject invalid or over-limit public snapshot sizes."""
    maximum = get_settings().file_download_max_file_bytes
    if size < 0:
        raise ValueError("File size is invalid")
    if maximum > 0 and size > maximum:
        raise ValueError(f"File is too large: {size}")
