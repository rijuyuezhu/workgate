"""Windows native service logfile retention at launcher startup."""

import contextlib
import subprocess
import sys
from pathlib import Path

import pytest

from workgate.utils import service_logging


def test_small_service_log_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(service_logging, "_MAX_STARTUP_LOG_BYTES", 128)
    monkeypatch.setattr(service_logging, "_RETAIN_LOG_BYTES", 64)
    path = tmp_path / "executor.log"
    path.write_text("recent failure\n")
    with path.open("a+", encoding="utf-8", buffering=1) as stream:
        service_logging.compact_service_log(stream)
        stream.write("new startup\n")
    assert path.read_text() == "recent failure\nnew startup\n"


def test_oversized_log_retains_recent_failure_and_parent_child_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(service_logging, "_MAX_STARTUP_LOG_BYTES", 160)
    monkeypatch.setattr(service_logging, "_RETAIN_LOG_BYTES", 80)
    path = tmp_path / "executor.log"
    path.write_text("old " * 200 + "\nrecent ⚠ failure\n", encoding="utf-8")

    with (
        path.open("a+", encoding="utf-8", buffering=1) as stream,
        contextlib.redirect_stdout(stream),
        contextlib.redirect_stderr(stream),
    ):
        service_logging.compact_service_log(stream)
        print("parent startup")
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; "
                "print('child startup', flush=True); "
                "print('child error', file=sys.stderr, flush=True)",
            ],
            stdout=sys.stdout,
            stderr=sys.stderr,
            check=True,
            timeout=15,
        )
        print("parent shutdown")

    output = path.read_text(encoding="utf-8")
    assert len(path.read_bytes()) < 160
    assert "recent ⚠ failure" in output
    assert "old " * 20 not in output
    assert "parent startup" in output
    assert "child startup" in output
    assert "child error" in output
    assert "parent shutdown" in output


def test_compact_preserves_valid_utf8_on_partial_char_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(service_logging, "_MAX_STARTUP_LOG_BYTES", 32)
    monkeypatch.setattr(service_logging, "_RETAIN_LOG_BYTES", 9)
    path = tmp_path / "control.log"
    path.write_text("x" * 100 + "⚠ error\n", encoding="utf-8")
    with path.open("a+", encoding="utf-8", buffering=1) as stream:
        service_logging.compact_service_log(stream)
    assert "error" in path.read_text(encoding="utf-8")
