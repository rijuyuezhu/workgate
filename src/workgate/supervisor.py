"""One small CLI parent for ordinary Control and Executor processes.

Only the direct role child inherits its private IPC socket. The Parent owns
process lifetime; the Child continues to own all runtime and configuration.
"""

import os
import selectors
import signal
import socket
import stat
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

_PARENT_FD = "WORKGATE_PARENT_IPC_FD"
_STOP_TIMEOUT_SECONDS = 5


def child_command(*args: str) -> list[str]:
    """Relaunch the same installed CLI, including frozen installations."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "workgate.main", *args]


def is_child() -> bool:
    """An environment marker alone cannot accidentally bypass supervision."""
    try:
        fd = int(os.environ[_PARENT_FD])
        return stat.S_ISSOCK(os.fstat(fd).st_mode)
    except KeyError, ValueError, OSError:
        return False


def request_restart() -> None:
    """Ask the owning Parent to restart this role, via inherited local IPC.

    Only a supervised Child can make this request. Authorization for exposing
    it to a UI or model tool belongs to the later configuration Apply feature.
    """
    if not is_child():
        raise RuntimeError("no Workgate Parent is managing this process")
    with socket.socket(fileno=os.dup(int(os.environ[_PARENT_FD]))) as channel:
        channel.settimeout(5)
        channel.sendall(b"restart\n")
        response = channel.recv(64)
    if response != b"accepted\n":
        raise RuntimeError("Workgate Parent did not accept the restart")


@dataclass(frozen=True)
class ChildCommand:
    role: str
    argv: tuple[str, ...]


@dataclass
class _Child:
    command: ChildCommand
    process: subprocess.Popen[bytes]
    channel: socket.socket
    buffer: bytes = b""


def _stop(child: _Child) -> None:
    if child.process.poll() is None:
        with suppress(ProcessLookupError):
            child.process.terminate()
    try:
        child.process.wait(timeout=_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            child.process.kill()
        child.process.wait()


def supervise(commands: list[ChildCommand]) -> int:
    """Run exactly one Parent, with one independently restartable Child/role."""
    if not commands or len({item.role for item in commands}) != len(commands):
        raise ValueError("each supervised role must be unique")

    # A restart must preserve startup ENV and CLI arguments. Children reread
    # their file-backed configuration because every restart is a fresh exec.
    environment = os.environ.copy()
    environment.pop(_PARENT_FD, None)
    children: dict[str, _Child] = {}
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def terminate(_signal: int, _frame: Any) -> None:
        raise SystemExit(143)

    with selectors.DefaultSelector() as selector:

        def spawn(command: ChildCommand) -> None:
            parent_sock, child_sock = socket.socketpair()
            try:
                process = subprocess.Popen(
                    command.argv,
                    env={**environment, _PARENT_FD: str(child_sock.fileno())},
                    pass_fds=(child_sock.fileno(),),
                )
            except BaseException:
                parent_sock.close()
                raise
            finally:
                child_sock.close()
            child = _Child(command, process, parent_sock)
            children[command.role] = child
            selector.register(parent_sock, selectors.EVENT_READ, command.role)

        def retire(child: _Child) -> None:
            selector.unregister(child.channel)
            child.channel.close()

        signal.signal(signal.SIGTERM, terminate)
        try:
            for command in commands:
                spawn(command)
            while True:
                for child in children.values():
                    exit_code = child.process.poll()
                    if exit_code is not None:
                        return 128 - exit_code if exit_code < 0 else exit_code
                for key, _events in selector.select(timeout=0.2):
                    role: str = key.data
                    child = children[role]
                    try:
                        data = child.channel.recv(128)
                    except ConnectionError:
                        data = b""
                    if not data:
                        # An exiting Child can close the IPC before poll sees it.
                        retire(child)
                        continue
                    child.buffer += data
                    if len(child.buffer) > 128:
                        raise RuntimeError("invalid Parent IPC message")
                    if b"\n" not in child.buffer:
                        continue
                    message, child.buffer = child.buffer.split(b"\n", 1)
                    if message != b"restart" or child.buffer:
                        raise RuntimeError("unsupported Parent IPC request")
                    child.channel.sendall(b"accepted\n")
                    _stop(child)
                    retire(child)
                    spawn(child.command)
        except KeyboardInterrupt:
            return 130
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
            for child in children.values():
                _stop(child)
                if child.channel.fileno() != -1:
                    with suppress(KeyError):
                        selector.unregister(child.channel)
                    child.channel.close()


def run_parent(commands: list[ChildCommand]) -> None:
    """CLI exit status mirrors the child, never a second runtime adapter."""
    raise SystemExit(supervise(commands))
