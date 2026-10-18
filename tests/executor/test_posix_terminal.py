"""Failure-path and lifecycle tests for the native POSIX tmux attachment."""

import errno
import fcntl
import os
import pty
import struct
import subprocess
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from workgate.platform.posix import terminal as posix_terminal
from workgate.protocol.terminal import TerminalBridgeNotFoundError


def _attach(*, alive=True, closed=False):
    attachment = object.__new__(posix_terminal.UnixTmuxAttach)
    attachment._closed = closed
    attachment.master_fd = 71
    attachment._write_lock = threading.Lock()
    attachment._close_lock = threading.Lock()
    cast(Any, attachment)._struct = struct
    cast(Any, attachment)._termios = SimpleNamespace(TIOCSWINSZ=0x1234)
    cast(Any, attachment)._fcntl = SimpleNamespace(ioctl=lambda *_args: None)
    process = SimpleNamespace(
        pid=123,
        poll=lambda: None if alive else 0,
        wait=lambda timeout: None,
    )
    cast(Any, attachment).process = process
    return attachment


def test_resize_checks_closed_state_and_signals_live_group(monkeypatch):
    attach = _attach()
    calls = []
    cast(Any, attach)._fcntl = SimpleNamespace(
        ioctl=lambda fd, operation, size: calls.append((fd, operation, size))
    )
    monkeypatch.setattr(
        posix_terminal.os, "killpg", lambda *args: calls.append(args)
    )
    assert attach.resize(90, 35)
    assert calls == [
        (71, 0x1234, struct.pack("HHHH", 35, 90, 0, 0)),
        (123, posix_terminal.signal.SIGWINCH),
    ]

    attach._closed = True
    with pytest.raises(TerminalBridgeNotFoundError):
        attach.resize(90, 35)


def test_resize_skips_exited_group_and_ignores_process_race(monkeypatch):
    attach = _attach(alive=False)
    calls = []
    monkeypatch.setattr(
        posix_terminal.os, "killpg", lambda *_: calls.append("signal")
    )
    assert attach.resize(120, 36)
    assert calls == []

    cast(Any, attach).process = SimpleNamespace(pid=123, poll=lambda: None)
    monkeypatch.setattr(
        posix_terminal.os,
        "killpg",
        lambda *_: (_ for _ in ()).throw(ProcessLookupError()),
    )
    assert attach.resize(120, 36)


@pytest.mark.parametrize(
    ("outcome", "alive", "expected"),
    [
        (b"hello", True, (b"hello", False)),
        (b"", True, (b"", True)),
        (BlockingIOError(), True, (b"", False)),
        (OSError(errno.EIO, "I/O error"), True, (b"", True)),
        (OSError(errno.EBADF, "bad fd"), True, (b"", True)),
    ],
)
def test_read_wait_handles_data_eof_and_pty_errors(
    monkeypatch, outcome, alive, expected
):
    attach = _attach(alive=alive)
    monkeypatch.setattr(
        posix_terminal.select, "select", lambda *_: ([71], [], [])
    )

    def read(_fd, _length):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(posix_terminal.os, "read", read)
    assert attach.read_wait(2048, 10) == expected


def test_read_wait_handles_timeout_closure_and_unexpected_oserror(monkeypatch):
    attach = _attach(alive=False)
    monkeypatch.setattr(
        posix_terminal.select, "select", lambda *_: ([], [], [])
    )
    assert attach.read_wait(2048, 10) == (b"", True)
    attach._closed = True
    assert attach.read_wait(2048, 10) == (b"", True)
    attach._closed = False
    monkeypatch.setattr(
        posix_terminal.select, "select", lambda *_: ([71], [], [])
    )
    monkeypatch.setattr(
        posix_terminal.os,
        "read",
        lambda *_: (_ for _ in ()).throw(
            OSError(errno.ENOMEM, "out of memory")
        ),
    )
    with pytest.raises(OSError, match="out of memory"):
        attach.read_wait(2048, 10)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (OSError(errno.EBADF, "bad fd"), TerminalBridgeNotFoundError),
        (OSError(errno.EIO, "gone"), TerminalBridgeNotFoundError),
        (OSError(errno.ENOMEM, "system error"), OSError),
        (0, OSError),
    ],
)
def test_write_all_distinguishes_closed_pty_from_other_errors(
    monkeypatch, outcome, expected
):
    attach = _attach()

    def write(_fd, _data):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(posix_terminal.os, "write", write)
    with pytest.raises(expected):
        attach.write_all(b"abc")
    attach._closed = True
    with pytest.raises(TerminalBridgeNotFoundError):
        attach.write_all(b"abc")
    attach.write_all(b"")


def test_close_is_idempotent_and_does_not_signal_exited_child(monkeypatch):
    attach = _attach(alive=False)
    closes = []
    monkeypatch.setattr(
        posix_terminal.os, "close", lambda fd: closes.append(fd)
    )
    monkeypatch.setattr(
        posix_terminal.os,
        "killpg",
        lambda *_: pytest.fail("exited child should not be signaled"),
    )
    attach.close_sync()
    attach.close_sync()
    assert closes == [71]
    assert attach.master_fd == -1


def test_close_escalates_after_term_timeout(monkeypatch):
    attach = _attach()
    calls = []

    def wait(timeout):
        calls.append(("wait", timeout))
        raise subprocess.TimeoutExpired("tmux", timeout)

    cast(Any, attach).process = SimpleNamespace(
        pid=123, poll=lambda: None, wait=wait
    )
    monkeypatch.setattr(
        posix_terminal.os, "close", lambda fd: calls.append(("close", fd))
    )
    monkeypatch.setattr(
        posix_terminal.os,
        "killpg",
        lambda pid, sig: calls.append(("signal", pid, sig)),
    )
    attach.close_sync()
    assert calls == [
        ("close", 71),
        ("signal", 123, posix_terminal.signal.SIGTERM),
        ("wait", 2),
        ("signal", 123, posix_terminal.signal.SIGKILL),
        ("wait", 2),
    ]


def test_constructor_closes_both_fds_if_pty_setup_fails(monkeypatch):
    monkeypatch.setattr(
        posix_terminal,
        "require_tmux",
        lambda: SimpleNamespace(path="/usr/bin/tmux"),
    )
    monkeypatch.setattr(
        posix_terminal.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=0),
    )
    closes = []
    monkeypatch.setattr(pty, "openpty", lambda: (71, 72))
    monkeypatch.setattr(fcntl, "ioctl", lambda *_a: None)
    monkeypatch.setattr(os, "close", lambda fd: closes.append(fd))
    monkeypatch.setattr(
        posix_terminal.UnixTmuxAttach,
        "resize",
        lambda *_a: (_ for _ in ()).throw(OSError("ioctl failed")),
    )
    with pytest.raises(OSError, match="ioctl failed"):
        posix_terminal.UnixTmuxAttach("demo", 90, 35)
    assert closes == [71, 72]


def test_constructor_closes_both_fds_if_nonblocking_setup_fails(monkeypatch):
    monkeypatch.setattr(
        posix_terminal,
        "require_tmux",
        lambda: SimpleNamespace(path="/usr/bin/tmux"),
    )
    monkeypatch.setattr(
        posix_terminal.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=0),
    )
    closes = []
    monkeypatch.setattr(pty, "openpty", lambda: (71, 72))
    monkeypatch.setattr(fcntl, "ioctl", lambda *_a: None)
    monkeypatch.setattr(os, "close", lambda fd: closes.append(fd))
    monkeypatch.setattr(
        os,
        "set_blocking",
        lambda *_a: (_ for _ in ()).throw(OSError("nonblocking failed")),
    )
    monkeypatch.setattr(
        posix_terminal.subprocess,
        "Popen",
        lambda *_a, **_kw: pytest.fail("child must not start before PTY setup"),
    )
    with pytest.raises(OSError, match="nonblocking failed"):
        posix_terminal.UnixTmuxAttach("demo", 90, 35)
    assert closes == [71, 72]
