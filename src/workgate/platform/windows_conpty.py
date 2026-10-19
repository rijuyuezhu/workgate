"""Small native ConPTY I/O core. Session ownership stays with the Executor."""

import threading
from typing import Any

_BUFFER_BYTES = 256 * 1024


class _Subscriber:
    """One bounded independent reader. Slow clients terminate, not block others."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.buffer = bytearray()
        self.finished = False

    def feed(self, data: bytes) -> None:
        with self.condition:
            if self.finished:
                return
            if len(self.buffer) + len(data) > _BUFFER_BYTES:
                self.buffer.clear()
                self.finished = True
            else:
                self.buffer.extend(data)
            self.condition.notify_all()

    def finish(self) -> None:
        with self.condition:
            self.finished = True
            self.condition.notify_all()

    def read_wait(self, max_bytes: int, wait_ms: int) -> tuple[bytes, bool]:
        with self.condition:
            if not self.buffer and not self.finished:
                self.condition.wait(max(0, wait_ms) / 1000)
            count = min(max(0, max_bytes), len(self.buffer))
            data = bytes(self.buffer[:count])
            del self.buffer[:count]
            return data, self.finished and not self.buffer


class ConPtySession:
    """One ConPTY child and one reader, shared by independent attachments."""

    def __init__(self, process: Any) -> None:
        self.process = process
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.subscribers: set[_Subscriber] = set()
        self.closed = False
        self.ended = False
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        try:
            while not self.closed and self.process.isalive():
                data = self.process.read()
                if not data:
                    continue
                chunk = (
                    data.encode("utf-8", errors="replace")
                    if isinstance(data, str)
                    else bytes(data)
                )
                with self.lock:
                    subscribers = tuple(self.subscribers)
                for subscriber in subscribers:
                    subscriber.feed(chunk)
        except EOFError, OSError:
            pass
        finally:
            with self.lock:
                self.ended = True
                subscribers = tuple(self.subscribers)
                self.subscribers.clear()
            for subscriber in subscribers:
                subscriber.finish()

    def subscribe(self) -> _Subscriber:
        with self.lock:
            if self.closed or self.ended or not self.process.isalive():
                raise RuntimeError("ConPTY session is unavailable")
            subscriber = _Subscriber()
            self.subscribers.add(subscriber)
            return subscriber

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        with self.lock:
            self.subscribers.discard(subscriber)
        subscriber.finish()

    def write(self, data: str) -> None:
        with self.write_lock:
            with self.lock:
                if self.closed:
                    raise RuntimeError("ConPTY session is unavailable")
            self.process.write(data)

    def resize(self, cols: int, rows: int) -> None:
        with self.write_lock:
            with self.lock:
                if self.closed:
                    raise RuntimeError("ConPTY session is unavailable")
            self.process.setwinsize(rows, cols)

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            subscribers = tuple(self.subscribers)
            self.subscribers.clear()
        for subscriber in subscribers:
            subscriber.finish()
        self.process.close(force=True)
        if self.reader is not threading.current_thread():
            self.reader.join(timeout=2)


def spawn_conpty(
    command: list[str], *, cwd: str, cols: int = 120, rows: int = 36
) -> ConPtySession:
    """Start one native Windows PTY, no fallback to an uncontained pipe."""
    from winpty import PtyProcess  # type: ignore[import-not-found]

    return ConPtySession(
        PtyProcess.spawn(command, cwd=cwd, dimensions=(rows, cols))
    )
