"""Temporary file helpers shared by executor operation implementations."""

import asyncio
import uuid
from pathlib import Path

from .path import assert_text_input_size, prune_temp_dir, temp_dir


async def write_temp_text_file(
    input_name: str,
    content: str,
    filename_prefix: str,
    suffix: str,
    *,
    max_input_bytes: int,
    max_tmp_files: int,
    max_tmp_bytes: int,
    temp_directory: Path,
) -> Path:
    """Validate and write text content to one explicit executor scratch directory."""
    assert_text_input_size(input_name, content, max_input_bytes)
    await asyncio.to_thread(
        prune_temp_dir,
        max_files=max_tmp_files,
        max_bytes=max_tmp_bytes,
        directory=temp_directory,
    )
    path = (
        temp_dir(temp_directory)
        / f"{filename_prefix}-{uuid.uuid4().hex}.{suffix}"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(
        path.write_text,
        content,
        encoding="utf-8",
        newline="",
    )
    return path
