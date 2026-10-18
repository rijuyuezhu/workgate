"""Portable private-reader size checks; native ACL reads have Windows smokes."""

import pytest

from workgate.utils.private_files import read_private_bytes, read_private_text


def test_private_reader_plain_bytes_text_and_bounded_length(tmp_path):
    source = tmp_path / "secret"
    source.write_bytes(b"secret")
    assert read_private_bytes(source) == b"secret"
    assert read_private_bytes(source, max_bytes=6) == b"secret"
    assert read_private_text(source, max_bytes=6) == "secret"
    with pytest.raises(ValueError, match="size limit"):
        read_private_bytes(source, max_bytes=5)


def test_private_reader_missing_path_surfaces_missing_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_private_bytes(tmp_path / "absent", max_bytes=64)
