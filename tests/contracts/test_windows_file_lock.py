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


@pytest.mark.parametrize(
    "error", [errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK]
)
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


def test_windows_private_files_delegate_to_verified_native_backend(
    monkeypatch, tmp_path
):
    from workgate.platform.windows import security

    monkeypatch.setattr(private_files, "host_os", lambda: HostOS.WINDOWS)
    calls = []

    def fake_open(path, *, create, exclusive=False, append=False):
        calls.append((path.name, create, exclusive))
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = private_files.os.O_RDWR | private_files.os.O_CREAT
        if append:
            flags = (
                private_files.os.O_WRONLY
                | private_files.os.O_APPEND
                | private_files.os.O_CREAT
            )
        return private_files.os.open(path, flags, 0o600)

    monkeypatch.setattr(security, "open_private_file", fake_open)
    monkeypatch.setattr(
        security, "replace_private_file", private_files.os.replace
    )
    monkeypatch.setattr(
        security,
        "delete_private_file",
        lambda path: path.unlink(missing_ok=True),
    )
    target = tmp_path / "secret"
    private_files.write_private_bytes(target, b"first")
    private_files.append_private_bytes(target, b"-append")
    private_files.atomic_write_private_bytes(target, b"replacement")
    assert target.read_bytes() == b"replacement"
    assert calls[0] == ("secret", True, False)
    assert calls[1] == ("secret", True, False)
    assert calls[2][1:] == (True, True)


def test_windows_insecure_private_lock_does_not_retry_forever(
    monkeypatch, tmp_path
):
    from workgate.platform.windows import security

    monkeypatch.setattr(private_files, "host_os", lambda: HostOS.WINDOWS)
    monkeypatch.setattr(
        security,
        "open_private_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("Windows private DACL mismatch")
        ),
    )
    with pytest.raises(PermissionError, match="DACL mismatch"):
        private_files._open_lock_handle(tmp_path / "insecure", timeout_s=0)


def test_windows_private_lock_file_is_verified_before_locking(
    monkeypatch, tmp_path
):
    from workgate.platform.windows import security

    monkeypatch.setattr(private_files, "host_os", lambda: HostOS.WINDOWS)
    calls = []

    def verified_open(path, *, create, exclusive=False, append=False):
        calls.append(("open", create, exclusive, append))
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = private_files.os.O_RDWR
        if create:
            flags |= private_files.os.O_CREAT
        if exclusive:
            flags |= private_files.os.O_EXCL
        return private_files.os.open(path, flags, 0o600)

    monkeypatch.setattr(security, "open_private_file", verified_open)
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(
            LK_NBLCK=6,
            LK_UNLCK=7,
            locking=lambda _fd, op, size: calls.append(("lock", op, size)),
        ),
    )
    path = tmp_path / "shard.lock"
    with private_files.private_file_lock(path) as handle:
        assert handle.read(1) == b"\0"
        assert calls[:2] == [
            ("open", True, False, False),
            ("open", False, False, False),
        ]
    assert calls[-2:] == [("lock", 6, 1), ("lock", 7, 1)]
    assert path.read_bytes() == b"\0"
    # Existing initialized sentinel must be reused without truncating it.
    with private_files.private_file_lock(path):
        pass
    assert path.read_bytes() == b"\0"


def test_windows_existing_insecure_lock_is_rejected_during_preparation(
    monkeypatch, tmp_path
):
    from workgate.platform.windows import security

    monkeypatch.setattr(private_files, "host_os", lambda: HostOS.WINDOWS)
    path = tmp_path / "unsafe.lock"
    path.write_bytes(b"\0")

    def reject(_path, **_kwargs):
        raise PermissionError("owner or DACL verification failed")

    monkeypatch.setattr(security, "open_private_file", reject)
    with (
        pytest.raises(PermissionError, match="DACL verification failed"),
        private_files.private_file_lock(path),
    ):
        pass
    assert path.read_bytes() == b"\0"


def test_windows_open_lock_retries_only_transient_missing_file(
    monkeypatch, tmp_path
):
    from workgate.platform.windows import security

    monkeypatch.setattr(private_files, "host_os", lambda: HostOS.WINDOWS)
    path = tmp_path / "lock"
    path.write_bytes(b"\0")
    calls = []

    def fake_open(_path, *, create, exclusive=False):
        calls.append(create)
        if len(calls) == 1:
            raise FileNotFoundError(
                "simulated concurrent lock-file initialization"
            )
        return private_files.os.open(path, private_files.os.O_RDWR)

    monkeypatch.setattr(security, "open_private_file", fake_open)
    with private_files._open_lock_handle(
        path, timeout_s=1, retry_interval_s=0
    ) as handle:
        assert handle.read(1) == b"\0"
    assert calls == [False, False]
