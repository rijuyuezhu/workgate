"""Small native ConPTY I/O core. Session ownership stays with the Executor."""

import threading
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_BUFFER_BYTES = 256 * 1024


def is_available() -> bool:
    """Report the installed Windows ConPTY dependency, not just host OS."""
    import os

    if os.name != "nt":
        return False
    try:
        from winpty import PtyProcess  # type: ignore[import-not-found]
    except ImportError:
        return False
    return hasattr(PtyProcess, "spawn")


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
        self.shell_id = ""
        self.cwd = ""
        self.command = ""
        self.owner: str | None = None
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.subscribers: set[_Subscriber] = set()
        self.closed = False
        self.ended = False
        self.tail = bytearray()
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
                    self.tail.extend(chunk)
                    del self.tail[: max(0, len(self.tail) - _BUFFER_BYTES)]
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
            subscriber.feed(bytes(self.tail))
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
        try:
            self.process.close(force=True)
        except Exception:
            with self.lock:
                self.closed = False
            raise
        if self.reader is not threading.current_thread():
            self.reader.join(timeout=2)


def spawn_conpty(
    command: list[str],
    *,
    cwd: str,
    cols: int = 120,
    rows: int = 36,
    env: dict[str, str] | None = None,
) -> ConPtySession:
    """Start one native Windows PTY, no fallback to an uncontained pipe."""
    from winpty import PtyProcess  # type: ignore[import-not-found]

    return ConPtySession(
        PtyProcess.spawn(command, cwd=cwd, dimensions=(rows, cols), env=env)
    )


class ConPtyRegistry:
    """Executor-owned ConPTY shells; intentionally not restart-durable."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.shells: dict[str, ConPtySession] = {}
        self.closed = False

    def active(self) -> dict[str, ConPtySession]:
        with self.lock:
            stale = [
                (name, shell)
                for name, shell in self.shells.items()
                if shell.ended
            ]
            active = {
                name: shell
                for name, shell in self.shells.items()
                if not shell.ended and not shell.closed
            }
        for name, shell in stale:
            shell.close()
            with self.lock:
                if self.shells.get(name) is shell:
                    self.shells.pop(name)
        return active

    def start(
        self,
        shell_id: str,
        command: list[str],
        *,
        cwd: str,
        limit: int,
        owner: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ConPtySession:
        with self.lock:
            if self.closed:
                raise RuntimeError("ConPTY runtime is closed")
            stale = [name for name, entry in self.shells.items() if entry.ended]
            for name in stale:
                self.shells[name].close()
                self.shells.pop(name)
            if shell_id in self.shells:
                raise RuntimeError(
                    f"Persistent shell already exists: {shell_id}"
                )
            if len(self.shells) >= limit:
                raise RuntimeError(
                    f"Persistent shell session limit {limit} reached"
                )
            session = spawn_conpty(command, cwd=cwd, env=env)
            session.shell_id = shell_id
            session.cwd = cwd
            session.command = " ".join(command)
            session.owner = owner
            self.shells[shell_id] = session
            return session

    def get(self, shell_id: str) -> ConPtySession:
        with self.lock:
            session = self.shells.get(shell_id)
            if session is None or session.ended or session.closed:
                raise RuntimeError(f"Persistent shell unavailable: {shell_id}")
            return session

    def kill(self, shell_id: str) -> bool:
        with self.lock:
            session = self.shells.get(shell_id)
        if session is None:
            return False
        session.close()
        with self.lock:
            if self.shells.get(shell_id) is session:
                self.shells.pop(shell_id)
        return True

    def close(self) -> None:
        with self.lock:
            self.closed = True
            sessions = tuple(self.shells.items())
        errors = []
        for name, session in sessions:
            try:
                session.close()
            except Exception as exc:
                errors.append(exc)
            else:
                with self.lock:
                    if self.shells.get(name) is session:
                        self.shells.pop(name)
        if errors:
            raise RuntimeError(
                f"Failed to close {len(errors)} ConPTY session(s)"
            ) from errors[0]


_CONPTY_REGISTRY: ContextVar[ConPtyRegistry | None] = ContextVar(
    "workgate_conpty_registry", default=None
)


@contextmanager
def use_conpty_registry(registry: ConPtyRegistry) -> Generator[None]:
    token = _CONPTY_REGISTRY.set(registry)
    try:
        yield
    finally:
        _CONPTY_REGISTRY.reset(token)


def conpty_registry() -> ConPtyRegistry:
    registry = _CONPTY_REGISTRY.get()
    if registry is None:
        raise RuntimeError("ConPTY runtime not bound to this Executor")
    return registry


class WindowsConPtyAttach:
    """Connection-scoped subscriber to an Executor-owned ConPTY shell."""

    backend = "conpty"

    def __init__(self, shell_id: str, cols: int, rows: int) -> None:
        import codecs

        self.shell_id = shell_id
        self._session = conpty_registry().get(shell_id)
        self._session.resize(cols, rows)
        self._subscriber = self._session.subscribe()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._closed = False
        self._lock = threading.Lock()

    def read_wait(self, max_bytes: int, wait_ms: int) -> tuple[bytes, bool]:
        return self._subscriber.read_wait(max_bytes, wait_ms)

    def write_all(self, data: bytes) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("ConPTY attachment is closed")
            text = self._decoder.decode(data)
            if text:
                self._session.write(text)

    def resize(self, cols: int, rows: int) -> bool:
        if self._closed:
            raise RuntimeError("ConPTY attachment is closed")
        self._session.resize(cols, rows)
        return True

    def close_sync(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._session.unsubscribe(self._subscriber)
