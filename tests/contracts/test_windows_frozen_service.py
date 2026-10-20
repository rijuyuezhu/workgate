"""Native smoke for the actual windowless, frozen Windows service executable."""

import json
import struct
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from workgate import __version__
from workgate.platform.windows_task import WindowsUserTask

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="native Windows only"
)


def _pe_subsystem(path: Path) -> int:
    # Read only PE headers; a PyInstaller one-file executable can be very large.
    with path.open("rb") as binary:
        dos_header = binary.read(64)
        assert dos_header[:2] == b"MZ"
        header_offset = struct.unpack_from("<I", dos_header, 0x3C)[0]
        binary.seek(header_offset)
        assert binary.read(4) == b"PE\x00\x00"
        binary.seek(header_offset + 4 + 20 + 68)
        return struct.unpack("<H", binary.read(2))[0]


def test_frozen_executor_windowless_task_smoke(tmp_path: Path) -> None:
    console = Path("dist/workgate.exe").resolve()
    service = Path("dist/workgatew.exe").resolve()
    assert console.is_file() and service.is_file()
    assert _pe_subsystem(console) == 3  # IMAGE_SUBSYSTEM_WINDOWS_CUI
    assert _pe_subsystem(service) == 2  # IMAGE_SUBSYSTEM_WINDOWS_GUI

    # Real Task Scheduler invocation must preserve quoted paths with spaces.
    working_dir = tmp_path / "task directory with spaces"
    working_dir.mkdir()
    output = working_dir / "service-version.txt"
    task = WindowsUserTask(
        "WG391-Disposable-" + uuid4().hex[:12],
        description="Disposable Workgate release smoke, safe to remove",
        run=lambda argv: subprocess.run(
            argv, check=True, capture_output=True, text=True, timeout=20
        ),
    )
    try:
        task.install(
            [str(service), "--_windows-version-file", str(output)],
            cwd=str(working_dir),
        )
        task.start()
        deadline = time.monotonic() + 45
        while not output.is_file() and time.monotonic() < deadline:
            time.sleep(0.15)
        assert output.is_file(), (
            f"windowless frozen service did not start: {task.status()}"
        )
        assert (
            output.read_text(encoding="ascii").strip()
            == f"workgate {__version__}"
        )
    finally:
        task.uninstall()


def test_windowless_executor_reports_startup_errors_in_service_log(
    tmp_path: Path,
) -> None:
    """The real GUI-subsystem binary must not silently lose service failures."""
    service = Path("dist/workgatew.exe").resolve()
    working_dir = tmp_path / "service directory with spaces"
    working_dir.mkdir()
    config = working_dir / "config.json"
    config.write_text(
        json.dumps(
            {
                "state_dir": str(tmp_path / "state"),
                "default_workdir": str(tmp_path),
            }
        )
    )
    log = working_dir / "executor.log"
    task = WindowsUserTask(
        "WG391-Disposable-" + uuid4().hex[:12],
        description="Disposable Workgate service log smoke, safe to remove",
        run=lambda argv: subprocess.run(
            argv, check=True, capture_output=True, text=True, timeout=20
        ),
    )
    try:
        task.install(
            [
                str(service),
                "executor",
                "run",
                "--managed-service",
                "--config",
                str(config),
            ],
            cwd=str(working_dir),
        )
        task.start()
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if log.is_file() and "executor command failed" in log.read_text(
                errors="replace"
            ):
                break
            time.sleep(0.2)
        assert log.is_file(), (
            f"windowless Executor created no service log: {task.status()}"
        )
        assert "executor command failed" in log.read_text(errors="replace")
    finally:
        task.uninstall()
