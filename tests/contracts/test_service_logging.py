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


def test_invalid_utf8_never_expands_retained_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(service_logging, "_MAX_STARTUP_LOG_BYTES", 128)
    monkeypatch.setattr(service_logging, "_RETAIN_LOG_BYTES", 64)
    path = tmp_path / "executor.log"
    path.write_bytes(b"\xff" * 256)
    with path.open("a+", encoding="utf-8", buffering=1) as stream:
        service_logging.compact_service_log(stream)
    assert len(path.read_bytes()) <= 64
    path.read_text(encoding="utf-8")


def test_failed_compaction_does_not_block_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    path = tmp_path / "control.log"
    path.write_text("last failure\n", encoding="utf-8")

    def fail_fstat(_fd: int):
        raise OSError("private path or filename should not leak")

    monkeypatch.setattr(service_logging.os, "fstat", fail_fstat)
    with path.open("a+", encoding="utf-8", buffering=1) as stream:
        service_logging.compact_service_log(stream)
        stream.write("service still starts\n")
    assert (
        path.read_text(encoding="utf-8")
        == "last failure\nservice still starts\n"
    )
    assert "Service log retention skipped (OSError)" in capsys.readouterr().err
