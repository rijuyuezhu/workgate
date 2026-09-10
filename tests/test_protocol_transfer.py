import pytest

from workgate.protocol.transfer import (
    DEFAULT_TRANSFER_CHUNK_BYTES,
    MAX_TRANSFER_CHUNK_BYTES,
    normalize_chunk_size,
)


def test_normalize_chunk_size_uses_default_and_caps_large_values() -> None:
    assert normalize_chunk_size() == DEFAULT_TRANSFER_CHUNK_BYTES
    assert normalize_chunk_size(1234) == 1234
    assert (
        normalize_chunk_size(MAX_TRANSFER_CHUNK_BYTES + 1)
        == MAX_TRANSFER_CHUNK_BYTES
    )


@pytest.mark.parametrize("value", [0, -1])
def test_normalize_chunk_size_rejects_non_positive_values(value: int) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        normalize_chunk_size(value)
