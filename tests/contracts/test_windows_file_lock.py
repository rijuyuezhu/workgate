"""Windows msvcrt byte-lock behavior is tested without importing it on POSIX."""

import errno
import sys
from types import SimpleNamespace

import pytest

from workgate.platform.host import HostOS
from workgate.platform.windows import locks
from workgate.utils import private_files


def test_windows_lock_and_unlock_use_initialized_first_byte(
    monkeypatch, tmp_path
):
    calls = []
    mock = SimpleNamespace(
        LK_NBLCK=6,
        LK_UNLCK=7,
        locking=lambda fd, op, length: calls.append((fd, op, length)),
    )
    monkeypatch.setitem(sys.modules, "msvcrt", mock)
    monkeypatch.setattr(private_files, "host_os", lambda: HostOS.WINDOWS)
    path = tmp_path / "lock"
    path.write_bytes(b"\0")
    with path.open("r+b") as handle:
        assert private_files._try_lock_handle(handle)
        private_files._unlock_handle(handle)
        assert calls == [
            (handle.fileno(), mock.LK_NBLCK, 1),
            (handle.fileno(), mock.LK_UNLCK, 1),
        ]


@pytest.mark.parametrize("error", [errno.EACCES, errno.EAGAIN])
def test_windows_lock_recognizes_byte_contention(monkeypatch, tmp_path, error):
    path = tmp_path / "lock"
    path.write_bytes(b"\0")

    def lock(_fd, _mode, _length):
        raise OSError(error, "lock contention")

    mock = SimpleNamespace(LK_NBLCK=6, locking=lock)
    monkeypatch.setitem(sys.modules, "msvcrt", mock)
    with path.open("r+b") as handle:
        assert not locks.try_lock_handle(handle)


def test_windows_lock_does_not_swallow_unexpected_errors(monkeypatch, tmp_path):
    path = tmp_path / "lock"
    path.write_bytes(b"\0")

    def lock(_fd, _mode, _length):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setitem(
        sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=6, locking=lock)
    )
    with path.open("r+b") as handle, pytest.raises(OSError, match="I/O error"):
        locks.try_lock_handle(handle)


def test_windows_private_files_fail_closed_without_acl_backend(
    monkeypatch, tmp_path
):
    from workgate.platform import files as platform_files

    monkeypatch.setattr(platform_files, "host_os", lambda: HostOS.WINDOWS)
    target = tmp_path / "password"
    with pytest.raises(PermissionError, match="user-only ACL"):
        private_files.write_private_bytes(target, b"secret")
    with pytest.raises(PermissionError, match="user-only ACL"):
        private_files.atomic_write_private_bytes(target, b"secret")
    with pytest.raises(PermissionError, match="user-only ACL"):
        private_files.append_private_bytes(target, b"secret")
    with (
        pytest.raises(PermissionError, match="user-only ACL"),
        private_files.private_file_lock(tmp_path / "lock"),
    ):
        pass
    assert not target.exists()
