"""Portable bounded stream-copy tests for native Windows directory staging."""

import io

import pytest

from workgate.platform.windows.archive import _copy_exact_file


def test_windows_staged_copy_exact_bytes() -> None:
    output = io.BytesIO()
    _copy_exact_file(io.BytesIO(b"payload"), output, 7)
    assert output.getvalue() == b"payload"


def test_windows_staged_copy_rejects_source_growth_without_reading_extra_bytes() -> (
    None
):
    output = io.BytesIO()
    with pytest.raises(RuntimeError, match="grew"):
        _copy_exact_file(io.BytesIO(b"payload-and-injected-append"), output, 7)
    assert output.getvalue() == b"payload"


def test_windows_staged_copy_rejects_source_truncation() -> None:
    output = io.BytesIO()
    with pytest.raises(RuntimeError, match="truncated"):
        _copy_exact_file(io.BytesIO(b"short"), output, 9)
    assert output.getvalue() == b"short"
