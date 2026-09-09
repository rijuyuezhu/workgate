"""Process-neutral transfer sizing contract shared by control and executors."""

DEFAULT_TRANSFER_CHUNK_BYTES = 1024 * 1024
MAX_TRANSFER_CHUNK_BYTES = 4 * 1024 * 1024


def normalize_chunk_size(chunk_size: int | None = None) -> int:
    """Clamp a requested transfer chunk size to the protocol-supported range."""
    requested = (
        DEFAULT_TRANSFER_CHUNK_BYTES if chunk_size is None else int(chunk_size)
    )
    if requested <= 0:
        raise ValueError("chunk_size must be greater than zero")
    return min(requested, MAX_TRANSFER_CHUNK_BYTES)
