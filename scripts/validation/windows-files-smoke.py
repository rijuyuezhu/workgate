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
from workgate.platform.windows.nt_relative import (
    close_handle,
    open_child,
    pinned_parent,
    rename_child,
)
from workgate.platform.windows.security import (
    open_regular_file_no_follow,
    replace_workspace_file,
)
from workgate.utils.private_files import (
    append_private_bytes,
    atomic_write_private_bytes,
    private_file_lock,
    write_private_bytes,
)


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
        private = ensure_private_directory(Path(tmp) / "private")
        assert ensure_private_directory(private) == private
        nested = ensure_private_directory(private / "new" / "nested")
        assert nested.is_dir()
        secret = private / "secret"
        write_private_bytes(secret, b"first")
        append_private_bytes(secret, b"-appended")
        assert secret.read_bytes() == b"first-appended"
        atomic_write_private_bytes(secret, b"replaced")
        assert secret.read_bytes() == b"replaced"
        path = private / "lock"
        with private_file_lock(path):
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
        assert path.read_bytes() == b"\0"
        with private_file_lock(path):
            pass
        unsafe = Path(tmp) / "unsafe"
        unsafe.write_bytes(b"unprotected")
        try:
            write_private_bytes(unsafe, b"must-not-write")
        except PermissionError:
            assert unsafe.read_bytes() == b"unprotected"
        else:
            raise AssertionError("Insecure existing file was modified")
        try:
            atomic_write_private_bytes(unsafe, b"must-not-publish")
        except PermissionError:
            assert unsafe.read_bytes() == b"unprotected"
        else:
            raise AssertionError(
                "Atomic publish replaced insecure existing file"
            )
        assert list(Path(tmp).glob(".unsafe.*.tmp")) == []
        staging = private / "transfer-staging"
        write_private_bytes(staging, b"from-staging")
        workspace_file = Path(tmp) / "workspace-destination"
        workspace_file.write_bytes(b"existing")
        replace_workspace_file(staging, workspace_file)
        assert not staging.exists()
        assert workspace_file.read_bytes() == b"from-staging"
        public = Path(tmp) / "source.txt"
        public.write_bytes(b"public")
        with os.fdopen(open_regular_file_no_follow(public), "rb") as handle:
            assert handle.read() == b"public"
        with os.fdopen(
            open_regular_file_no_follow(public, writable=True), "r+b"
        ) as handle:
            handle.seek(0, os.SEEK_END)
            handle.write(b"-extended")
        assert public.read_bytes() == b"public-extended"
        symlink = Path(tmp) / "source-link"
        try:
            symlink.symlink_to(public)
        except OSError:
            print("WARNING: native file symlink unavailable")
        else:
            try:
                open_regular_file_no_follow(symlink)
            except PermissionError:
                pass
            else:
                raise AssertionError("Windows transfer followed a file symlink")
            symlink.unlink()
        junction = Path(tmp) / "junction"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(private)],
            capture_output=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 0:
            try:
                ensure_private_directory(junction)
            except PermissionError:
                pass
            else:
                raise AssertionError(
                    "Reparse point accepted as private directory"
                )
            try:
                write_private_bytes(junction / "must-not-appear", b"blocked")
            except PermissionError:
                assert not (private / "must-not-appear").exists()
            else:
                raise AssertionError(
                    "Windows parent junction permitted private write"
                )
            try:
                open_regular_file_no_follow(junction / "secret")
            except PermissionError:
                pass
            else:
                raise AssertionError(
                    "Windows parent junction permitted transfer read"
                )
            junction.rmdir()
        else:
            print("WARNING: junction test unavailable")
    with tempfile.TemporaryDirectory(prefix="workgate-parent-swap-") as temp:
        original = ensure_private_directory(Path(temp) / "original")
        write_private_bytes(original / "source", b"original-secret")
        write_private_bytes(original / "replacement", b"new-secure-secret")
        with pinned_parent(original / "source") as parent:
            moved = Path(temp) / "moved"
            original.rename(moved)
            original.mkdir()
            (original / "source").write_bytes(b"attacker-substitute")
            imported = open_child(
                parent, "source", access=0x80000000, directory=False
            )
            import msvcrt

            with os.fdopen(
                msvcrt.open_osfhandle(imported, os.O_RDONLY | os.O_BINARY), "rb"
            ) as source:
                assert source.read() == b"original-secret"
            rename_source = open_child(
                parent,
                "replacement",
                access=0x80000000 | 0x10000,
                directory=False,
            )
            from workgate.platform.windows.security import _libs

            kernel, _ = _libs()
            try:
                rename_child(rename_source, parent, "source")
            finally:
                close_handle(kernel, rename_source)
            assert (moved / "source").read_bytes() == b"new-secure-secret"
            assert (original / "source").read_bytes() == b"attacker-substitute"
    print(
        "Windows native ACLs, atomic files, locks, reparse and transfer handles: PASS"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
