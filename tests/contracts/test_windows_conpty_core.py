"""ConPTY shared-reader semantics without requiring a Windows test host."""

import threading

import pytest

from workgate.platform.windows_conpty import ConPtySession, _Subscriber


def test_subscriber_preserves_partial_reads_and_eof():
    subscriber = _Subscriber()
    subscriber.feed(b"hello world")
    assert subscriber.read_wait(5, 0) == (b"hello", False)
    subscriber.finish()
    assert subscriber.read_wait(20, 0) == (b" world", True)
    assert subscriber.read_wait(20, 0) == (b"", True)


def test_slow_subscriber_does_not_block_others():
    slow, fast = _Subscriber(), _Subscriber()
    slow.feed(b"x" * (256 * 1024 + 1))
    fast.feed(b"hello")
    assert slow.read_wait(8, 0) == (b"", True)
    assert fast.read_wait(8, 0) == (b"hello", False)


class FakePty:
    def __init__(self):
        self.alive = True
        self.entered = threading.Event()
        self.input = []
        self.size = []

    def isalive(self):
        return self.alive

    def read(self):
        self.entered.wait(timeout=1)
        self.entered.clear()
        return "first\n" if self.alive else ""

    def write(self, text):
        self.input.append(text)

    def setwinsize(self, rows, cols):
        self.size.append((rows, cols))

    def close(self, force):
        assert force
        self.alive = False
        self.entered.set()


def test_conpty_session_detach_keeps_process_and_close_ends_readers():
    process = FakePty()
    session = ConPtySession(process)
    a, b = session.subscribe(), session.subscribe()
    process.entered.set()
    assert a.read_wait(64, 1000)[0] == b"first\n"
    assert b.read_wait(64, 1000)[0] == b"first\n"
    session.unsubscribe(a)
    assert process.isalive()
    session.write("echo 1\r")
    session.resize(100, 35)
    assert process.input == ["echo 1\r"]
    assert process.size == [(35, 100)]
    session.close()
    _, eof = b.read_wait(1024, 100)
    assert eof


def test_natural_conpty_exit_still_closes_native_resources():
    process = FakePty()
    session = ConPtySession(process)
    attached = session.subscribe()
    process.alive = False
    process.entered.set()
    session.reader.join(timeout=2)
    assert not session.reader.is_alive()
    assert session.ended and not session.closed
    assert attached.read_wait(64, 0)[1]
    session.close()
    assert session.closed
    with pytest.raises(RuntimeError, match="unavailable"):
        session.subscribe()
