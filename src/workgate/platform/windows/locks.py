"""Native Windows one-byte advisory exclusion on private lock handles."""

from typing import Any, cast


def try_lock_handle(handle) -> bool:
    import msvcrt

    handle.seek(0)
    try:
        cast(Any, msvcrt).locking(
            handle.fileno(), cast(Any, msvcrt).LK_NBLCK, 1
        )
    except OSError as exc:
        if exc.errno in {13, 11, 36} or getattr(exc, "winerror", None) in {
            32,
            33,
            36,
        }:
            return False
        raise
    return True


def unlock_handle(handle) -> None:
    import msvcrt

    handle.seek(0)
    cast(Any, msvcrt).locking(handle.fileno(), cast(Any, msvcrt).LK_UNLCK, 1)
