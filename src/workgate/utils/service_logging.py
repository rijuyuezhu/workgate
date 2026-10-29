"""Bound historical Windows service diagnostics before a new launcher starts."""

import io
import os
from typing import cast

_MAX_STARTUP_LOG_BYTES = 4 * 1024 * 1024
_RETAIN_LOG_BYTES = 2 * 1024 * 1024


def compact_service_log(stream: io.TextIOWrapper) -> None:
    """Keep recent diagnostics in-place, before the launcher spawns any children.

    This caps retained history *at startup*, not output from a long-lived service.
    Never compact after a Child has inherited this file handle.
    """
    stream.flush()
    size = os.fstat(stream.fileno()).st_size
    if size <= _MAX_STARTUP_LOG_BYTES:
        return
    raw = cast(io.BufferedRandom, stream.buffer)
    raw.seek(size - _RETAIN_LOG_BYTES)
    recent = raw.read(_RETAIN_LOG_BYTES)
    # Never leave a partial UTF-8 character at the front of the retained tail.
    recent = recent.decode("utf-8", errors="replace").encode("utf-8")
    raw.seek(0)
    raw.truncate()
    raw.write(recent)
    raw.flush()
    stream.seek(0, os.SEEK_END)
