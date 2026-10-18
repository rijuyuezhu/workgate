"""Windows locks use Python's stdlib msvcrt, not native NT APIs."""

import errno
import sys
from types import SimpleNamespace

from workgate.platform.host import HostOS
from workgate.utils import private_files


def test_msvcrt_one_byte_lock_and_unlock(monkeypatch, tmp_path):
    calls = []
    stub = SimpleNamespace(
        LK_NBLCK=2,
        LK_UNLCK=3,
        locking=lambda fd, op, size: calls.append((fd, op, size)),
    )
    monkeypatch.setattr(private_files, "host_os", lambda: HostOS.WINDOWS)
    monkeypatch.setitem(sys.modules, "msvcrt", stub)
    path = tmp_path / "lock"
    path.write_bytes(b"\0")
    with path.open("r+b") as handle:
        assert private_files._try_lock_handle(handle)
        private_files._unlock_handle(handle)
        assert calls == [
            (handle.fileno(), stub.LK_NBLCK, 1),
            (handle.fileno(), stub.LK_UNLCK, 1),
        ]


def test_msvcrt_lock_contention(monkeypatch, tmp_path):
    def contend(_fd, _op, _size):
        raise OSError(errno.EACCES, "occupied")

    monkeypatch.setattr(private_files, "host_os", lambda: HostOS.WINDOWS)
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(LK_NBLCK=2, locking=contend),
    )
    path = tmp_path / "lock"
    path.write_bytes(b"\0")
    with path.open("r+b") as handle:
        assert not private_files._try_lock_handle(handle)
