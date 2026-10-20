"""Native smoke for the actual windowless, frozen Windows service executable."""

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
    binary = path.read_bytes()
    assert binary[:2] == b"MZ"
    header = struct.unpack_from("<I", binary, 0x3C)[0]
    assert binary[header : header + 4] == b"PE\x00\x00"
    return struct.unpack_from("<H", binary, header + 4 + 20 + 68)[0]


def test_frozen_executor_windowless_task_smoke(tmp_path: Path) -> None:
    console = Path("dist/workgate.exe").resolve()
    service = Path("dist/workgatew.exe").resolve()
    assert console.is_file() and service.is_file()
    assert _pe_subsystem(console) == 3  # IMAGE_SUBSYSTEM_WINDOWS_CUI
    assert _pe_subsystem(service) == 2  # IMAGE_SUBSYSTEM_WINDOWS_GUI

    output = tmp_path / "service-version.txt"
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
            cwd=str(tmp_path),
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
