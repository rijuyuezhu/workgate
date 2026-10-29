"""Bound historical Windows service diagnostics before a new launcher starts."""

import io
import os
import sys
from typing import cast

_MAX_STARTUP_LOG_BYTES = 4 * 1024 * 1024
_RETAIN_LOG_BYTES = 2 * 1024 * 1024


def compact_service_log(stream: io.TextIOWrapper) -> None:
    """Keep recent diagnostics in-place, before the launcher spawns any children.

    This caps retained history *at startup*, not output from a long-lived service.
    Never compact after a Child has inherited this file handle.
    """
    try:
        stream.flush()
        size = os.fstat(stream.fileno()).st_size
        if size <= _MAX_STARTUP_LOG_BYTES:
            return
        raw = cast(io.BufferedRandom, stream.buffer)
        raw.seek(size - _RETAIN_LOG_BYTES)
        recent = raw.read(_RETAIN_LOG_BYTES)
        # Decode for a valid UTF-8 boundary. Replacements may grow the tail.
        recent = recent.decode("utf-8", errors="replace").encode("utf-8")
        if len(recent) > _RETAIN_LOG_BYTES:
            recent = (
                recent[-_RETAIN_LOG_BYTES:]
                .decode("utf-8", errors="ignore")
                .encode("utf-8")
            )
        raw.seek(0)
        raw.truncate()
        raw.write(recent)
        raw.flush()
        stream.seek(0, os.SEEK_END)
    except OSError as exc:
        # Optional retention must not prevent a managed service from starting.
        print(
            f"Service log retention skipped ({type(exc).__name__})",
            file=sys.stderr,
        )
