"""Process-neutral bounded job-log helpers."""

from typing import BinaryIO


def compact_log(handle: BinaryIO, max_bytes: int) -> bool:
    """Keep only the newest max_bytes of one open binary job log."""
    handle.flush()
    size = handle.tell()
    if size <= max_bytes:
        return False
    handle.seek(max(0, size - max_bytes))
    tail = handle.read(max_bytes)
    handle.seek(0)
    handle.truncate()
    handle.write(tail)
    handle.flush()
    return True
