"""Native Windows filesystem smoke for WorkGate; Python stdlib only.

Run from an actual Windows host with Python 3.14:
  python scripts/validation/windows-files-smoke.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

if sys.platform != "win32":
    raise SystemExit("Windows-native smoke requires Windows CPython")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workgate.app_paths import ensure_private_directory, resolve_app_paths
from workgate.platform.path import path_lock_key, remote_path_name
from workgate.platform.windows.locks import try_lock_handle, unlock_handle
from workgate.utils.private_files import atomic_write_private_bytes


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--check-held-lock":
        with Path(sys.argv[2]).open("r+b") as handle:
            acquired = try_lock_handle(handle)
            if acquired:
                unlock_handle(handle)
        return 1 if acquired else 0

    paths = resolve_app_paths()
    assert paths.config_dir.is_absolute()
    assert paths.state_dir.is_absolute()
    assert (
        str(paths.config_dir)
        .casefold()
        .startswith(os.environ["APPDATA"].casefold())
    )
    assert (
        str(paths.state_dir)
        .casefold()
        .startswith(os.environ["LOCALAPPDATA"].casefold())
    )
    assert (
        str(paths.data_dir)
        .casefold()
        .startswith(os.environ["LOCALAPPDATA"].casefold())
    )
    assert paths.runtime_dir.is_absolute()
    assert remote_path_name(r"C:\Projects\src\report.txt") == "report.txt"
    assert path_lock_key(Path(r"C:\Projects\a\File.txt")) == path_lock_key(
        Path(r"c:/projects/A/FILE.TXT")
    )

    with tempfile.TemporaryDirectory(prefix="workgate-native-files-") as tmp:
        path = Path(tmp) / "lock"
        path.write_bytes(b"\0")
        with path.open("r+b") as handle:
            assert try_lock_handle(handle)
            child = subprocess.run(
                [sys.executable, __file__, "--check-held-lock", str(path)],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            assert child.returncode == 0, (
                child.returncode,
                child.stdout,
                child.stderr,
            )
            unlock_handle(handle)
        with path.open("r+b") as handle:
            assert try_lock_handle(handle)
            unlock_handle(handle)

        # Until Windows ACL support is implemented, never create unprotected
        # private data. This must be replaced by positive ACL checks in #383.
        for operation in (
            lambda: atomic_write_private_bytes(Path(tmp) / "secret", b"secret"),
            lambda: ensure_private_directory(Path(tmp) / "secret-dir"),
        ):
            try:
                operation()
            except PermissionError as exc:
                assert "ACL" in str(exc)
            else:
                raise AssertionError(
                    "Windows private data was written without ACL"
                )

    print("Windows native paths, locks, and fail-closed security: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
