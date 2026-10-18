"""Shared capability contracts, independent of native implementation."""

from typing import Protocol


class TerminalAttachment(Protocol):
    """Connection-scoped byte stream attached to a persistent shell.

    Closing an attachment must not terminate the underlying persistent shell.
    """

    backend: str
    shell_id: str

    def read_wait(self, max_bytes: int, wait_ms: int) -> tuple[bytes, bool]: ...

    def write_all(self, data: bytes) -> None: ...

    def resize(self, cols: int, rows: int) -> bool: ...

    def close_sync(self) -> None: ...
