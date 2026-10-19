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


def test_registry_prunes_naturally_finished_shells(monkeypatch):
    from workgate.platform import windows_conpty as conpty

    registry = conpty.ConPtyRegistry()
    processes = []

    def fake_spawn(_command, **_kwargs):
        proc = FakePty()
        processes.append(proc)
        return ConPtySession(proc)

    monkeypatch.setattr(conpty, "spawn_conpty", fake_spawn)
    first = registry.start("a", ["powershell"], cwd=".", limit=1)
    with pytest.raises(RuntimeError, match="limit"):
        registry.start("b", ["powershell"], cwd=".", limit=1)
    processes[0].alive = False
    processes[0].entered.set()
    first.reader.join(timeout=2)
    assert registry.active() == {}
    assert first.closed
    second = registry.start("b", ["powershell"], cwd=".", limit=1)
    assert registry.get("b") is second
    registry.close()
    assert not processes[1].alive
    with pytest.raises(RuntimeError, match="closed"):
        registry.start("c", ["powershell"], cwd=".", limit=1)


def test_registry_owner_and_independent_shells(monkeypatch):
    from workgate.platform import windows_conpty as conpty

    registry = conpty.ConPtyRegistry()
    monkeypatch.setattr(
        conpty,
        "spawn_conpty",
        lambda _command, **_kwargs: ConPtySession(FakePty()),
    )
    a = registry.start("a", ["powershell"], cwd=".", limit=2, owner="s1")
    b = registry.start("b", ["powershell"], cwd=".", limit=2, owner="s2")
    assert a.owner == "s1" and b.owner == "s2"
    with pytest.raises(RuntimeError, match="already exists"):
        registry.start("b", ["powershell"], cwd=".", limit=2)
    assert registry.kill("a")
    assert "a" not in registry.active()
    assert registry.get("b") is b
    registry.close()


def test_conpty_kill_retains_reference_if_native_close_fails(monkeypatch):
    from workgate.platform import windows_conpty as conpty

    registry = conpty.ConPtyRegistry()
    monkeypatch.setattr(
        conpty,
        "spawn_conpty",
        lambda *_a, **_kw: ConPtySession(FakePty()),
    )
    session = registry.start("retry", ["powershell"], cwd=".", limit=1)
    original_close = session.close

    def fail_close():
        raise OSError("ConPTY close failed")

    monkeypatch.setattr(session, "close", fail_close)
    with pytest.raises(OSError, match="ConPTY close failed"):
        registry.kill("retry")
    assert registry.get("retry") is session
    monkeypatch.setattr(session, "close", original_close)
    assert registry.kill("retry")
    assert "retry" not in registry.active()


def test_conpty_registry_closes_remaining_shells_on_one_failure(monkeypatch):
    from workgate.platform import windows_conpty as conpty

    registry = conpty.ConPtyRegistry()
    monkeypatch.setattr(
        conpty,
        "spawn_conpty",
        lambda *_a, **_kw: ConPtySession(FakePty()),
    )
    first = registry.start("first", ["powershell"], cwd=".", limit=2)
    second = registry.start("second", ["powershell"], cwd=".", limit=2)
    original_close = first.close
    monkeypatch.setattr(
        first,
        "close",
        lambda: (_ for _ in ()).throw(OSError("simulated close failure")),
    )
    with pytest.raises(RuntimeError, match="Failed to close 1"):
        registry.close()
    assert second.closed
    assert registry.shells == {"first": first}
    monkeypatch.setattr(first, "close", original_close)
    registry.close()
    assert registry.shells == {}


def test_attachment_decodes_utf8_split_across_wire_frames(monkeypatch):
    from workgate.platform import windows_conpty as conpty

    registry = conpty.ConPtyRegistry()
    process = FakePty()
    monkeypatch.setattr(
        conpty,
        "spawn_conpty",
        lambda *_a, **_kw: ConPtySession(process),
    )
    with conpty.use_conpty_registry(registry):
        registry.start("utf8", ["powershell"], cwd=".", limit=2)
        attachment = conpty.WindowsConPtyAttach("utf8", 80, 24)
        payload = "你好".encode()
        attachment.write_all(payload[:2])
        assert process.input == []
        attachment.write_all(payload[2:])
        assert process.input == ["你好"]
        attachment.close_sync()
        assert registry.get("utf8").process.isalive()
    registry.close()


def test_conpty_attachment_resize_read_and_closed_errors(monkeypatch):
    from workgate.platform import windows_conpty as conpty

    registry = conpty.ConPtyRegistry()
    proc = FakePty()
    monkeypatch.setattr(
        conpty,
        "spawn_conpty",
        lambda *_a, **_kw: ConPtySession(proc),
    )
    with conpty.use_conpty_registry(registry):
        registry.start("terminal", ["powershell"], cwd=".", limit=1)
        attach = conpty.WindowsConPtyAttach("terminal", 80, 24)
        attach._subscriber.feed(b"hello")
        assert attach.read_wait(2, 0) == (b"he", False)
        assert attach.resize(90, 30)
        assert proc.size[-1] == (30, 90)
        attach.close_sync()
        attach.close_sync()
        with pytest.raises(RuntimeError, match="closed"):
            attach.write_all(b"x")
        with pytest.raises(RuntimeError, match="closed"):
            attach.resize(80, 24)
        with pytest.raises(RuntimeError, match="unavailable"):
            registry.get("missing")
    registry.close()
    with pytest.raises(RuntimeError, match="bound"):
        conpty.conpty_registry()


def test_late_conpty_attach_replays_bounded_tail(monkeypatch):

    proc = FakePty()
    session = ConPtySession(proc)
    with session.lock:
        session.tail.extend(b"earlier output")
    late = session.subscribe()
    assert late.read_wait(100, 0) == (b"earlier output", False)
    late.feed(b" latest")
    assert late.read_wait(100, 0) == (b" latest", False)
    session.close()


def test_concurrent_native_close_failure_cannot_drop_live_session(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from workgate.platform import windows_conpty as conpty

    class FailOncePty(FakePty):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls = 0

        def close(self, force):
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                assert self.release.wait(timeout=3)
                raise OSError("native close failed")
            return super().close(force)

    registry = conpty.ConPtyRegistry()
    process = FailOncePty()
    monkeypatch.setattr(
        conpty,
        "spawn_conpty",
        lambda *_a, **_kw: ConPtySession(process),
    )
    registry.start("race", ["powershell"], cwd=".", limit=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(registry.kill, "race")
        assert process.started.wait(timeout=3)
        second = pool.submit(registry.kill, "race")
        process.release.set()
        with pytest.raises(OSError, match="native close failed"):
            first.result(timeout=5)
        assert second.result(timeout=5)
    assert process.calls == 2
    assert not registry.active()
