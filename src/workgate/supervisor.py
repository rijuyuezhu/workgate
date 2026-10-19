"""One small CLI parent for ordinary Control and Executor processes.

Only the direct role child inherits its private IPC socket. The Parent owns
process lifetime; the Child continues to own all runtime and configuration.
"""

import argparse
import hashlib
import json
import os
import selectors
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PARENT_FD_ENV = "WORKGATE_PARENT_IPC_FD"
_STOP_TIMEOUT_SECONDS = 3
_APPLY_TIMEOUT_SECONDS = 20
_CHANNEL_LOCK = threading.Lock()


def child_command(*args: str) -> list[str]:
    """Relaunch the same installed CLI, including frozen installations."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "workgate.main", *args]


def is_child() -> bool:
    """An environment marker alone cannot accidentally bypass supervision."""
    try:
        fd = int(os.environ[PARENT_FD_ENV])
        return stat.S_ISSOCK(os.fstat(fd).st_mode)
    except KeyError, ValueError, OSError:
        return False


def supervise_cli_role(role: str, args: argparse.Namespace) -> bool:
    """Run a root CLI role under Parent, never nesting inside a Child.

    Direct CLI handler calls without root argv remain usable for tests.
    """
    arguments = getattr(args, "_workgate_argv", None)
    if arguments is None or is_child():
        return False
    run_parent([ChildCommand(role, tuple(child_command(*arguments)))])
    return True


def _parent_message(message: bytes) -> bytes:
    """Serialize all queries over the single inherited Child/Parent socket."""
    with (
        _CHANNEL_LOCK,
        socket.socket(fileno=os.dup(int(os.environ[PARENT_FD_ENV]))) as channel,
    ):
        channel.settimeout(5)
        channel.sendall(message)
        response = channel.recv(64)
    return response


def request_restart(expected_revision: str | None = None) -> None:
    """Ask the owning Parent to restart this role, via inherited local IPC.

    Only a supervised Child can make this request. Authorization for exposing
    it to a UI or model tool belongs to the later configuration Apply feature.
    """
    if not is_child():
        raise RuntimeError("no Workgate Parent is managing this process")
    if expected_revision is not None and (
        len(expected_revision) != 64
        or any(char not in "0123456789abcdef" for char in expected_revision)
    ):
        raise ValueError("Invalid config revision")
    message = (
        ("restart:" + expected_revision).encode() + b"\n"
        if expected_revision
        else b"restart\n"
    )
    response = _parent_message(message)
    if response != b"accepted\n":
        raise RuntimeError("Workgate Parent did not accept the restart")


def notify_ready(
    config_path: Path | None = None, file_digest: str | None = None
) -> None:
    """Tell the owning Parent this Child has completed startup.

    The Parent can now record the *running* configuration as last-known-good.
    Unsupervised direct handler calls have no Parent to notify.
    """
    if not is_child():
        return
    selected = str(config_path) if config_path is not None else None
    message = (
        json.dumps({"ready": selected, "digest": file_digest}).encode("utf-8")
        + b"\n"
    )
    if len(message) > 8192:
        raise RuntimeError("configuration path is too long")
    if _parent_message(message) != b"accepted\n":
        raise RuntimeError("Workgate Parent did not confirm Child readiness")


def restart_status() -> str:
    """Read the Parent's state for this role, without another external endpoint."""
    if not is_child():
        return "unmanaged"
    status = _parent_message(b"status\n").strip().decode("ascii")
    if status not in {"starting", "healthy", "applying", "recovered", "shared"}:
        raise RuntimeError("invalid Parent status")
    return status


@dataclass(frozen=True)
class ChildCommand:
    role: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class _GoodFile:
    path: Path
    content: bytes


@dataclass
class _Applying:
    candidate: bytes | None
    good: _GoodFile | None
    started_at: float
    recovering: bool = False


def _snapshot_file(value: str | None) -> _GoodFile | None:
    """Snapshot only a regular owner-selected file of a successfully started Child."""
    if not value:
        return None
    path = Path(value)
    try:
        info = path.lstat()
        if (
            not path.is_absolute()
            or path.resolve() != path.absolute()
            or not stat.S_ISREG(info.st_mode)
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        ):
            return None
        return _GoodFile(path, path.read_bytes())
    except OSError:
        return None


def _restore_good(good: _GoodFile | None, candidate: bytes | None) -> bool:
    """Never overwrite a file modified again while Apply was in progress."""
    if good is None or candidate is None:
        return False
    from .utils.private_files import (
        atomic_write_private_bytes,
        private_file_lock,
    )

    try:
        if _snapshot_file(str(good.path)) != _GoodFile(good.path, candidate):
            return False
        lock = good.path.with_name(f".{good.path.name}.lock")
        with private_file_lock(lock, timeout_s=5):
            if _snapshot_file(str(good.path)) != _GoodFile(
                good.path, candidate
            ):
                return False
            atomic_write_private_bytes(good.path, good.content)
        return True
    except OSError, TimeoutError:
        return False


@dataclass
class _Child:
    command: ChildCommand
    process: subprocess.Popen[bytes]
    channel: socket.socket
    buffer: bytes = b""
    status: str = "starting"


def _stop(children: list[_Child]) -> None:
    """Signal every Child first, then wait within one shared grace period."""
    deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
    for child in children:
        if child.process.poll() is None:
            with suppress(ProcessLookupError):
                child.process.terminate()
    for child in children:
        try:
            child.process.wait(timeout=max(0, deadline - time.monotonic()))
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
    environment.pop(PARENT_FD_ENV, None)
    children: dict[str, _Child] = {}
    good_files: dict[str, _GoodFile] = {}
    applying: dict[str, _Applying] = {}
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)

    def terminate(_signal: int, _frame: Any) -> None:
        raise SystemExit(143)

    with selectors.DefaultSelector() as selector:

        def spawn(command: ChildCommand, *, status: str = "starting") -> None:
            parent_sock, child_sock = socket.socketpair()
            try:
                process = subprocess.Popen(
                    command.argv,
                    env={
                        **environment,
                        PARENT_FD_ENV: str(child_sock.fileno()),
                    },
                    pass_fds=(child_sock.fileno(),),
                )
            except BaseException:
                parent_sock.close()
                raise
            finally:
                child_sock.close()
            child = _Child(command, process, parent_sock, status=status)
            children[command.role] = child
            selector.register(parent_sock, selectors.EVENT_READ, command.role)

        def retire(child: _Child) -> None:
            if child.channel.fileno() >= 0:
                selector.unregister(child.channel)
                child.channel.close()

        def shared_config(role: str) -> bool:
            own = good_files.get(role)
            return own is not None and any(
                other != role and good.path == own.path
                for other, good in good_files.items()
            )

        def recover(role: str, child: _Child) -> bool:
            progress = applying.get(role)
            if progress is None or progress.recovering:
                return False
            if not _restore_good(progress.good, progress.candidate):
                return False
            progress.recovering = True
            progress.started_at = time.monotonic()
            retire(child)
            spawn(child.command, status="applying")
            return True

        signal.signal(signal.SIGTERM, terminate)
        try:
            for command in commands:
                spawn(command)
            while True:
                for role, child in tuple(children.items()):
                    exit_code = child.process.poll()
                    if exit_code is not None:
                        if recover(role, child):
                            continue
                        return 128 - exit_code if exit_code < 0 else exit_code
                    pending = applying.get(role)
                    if (
                        pending
                        and time.monotonic() - pending.started_at
                        > _APPLY_TIMEOUT_SECONDS
                    ):
                        _stop([child])
                        if recover(role, child):
                            continue
                        return 1
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
                    if len(child.buffer) > 8192:
                        raise RuntimeError("invalid Parent IPC message")
                    if b"\n" not in child.buffer:
                        continue
                    message, child.buffer = child.buffer.split(b"\n", 1)
                    if child.buffer:
                        raise RuntimeError("unsupported Parent IPC request")
                    if message == b"status":
                        state = (
                            "shared" if shared_config(role) else child.status
                        )
                        child.channel.sendall(state.encode("ascii") + b"\n")
                        continue
                    if message.startswith(b'{"ready": '):
                        payload = json.loads(message)
                        selected = payload["ready"]
                        digest = payload["digest"]
                        if selected is not None and not isinstance(
                            selected, str
                        ):
                            raise RuntimeError("invalid ready path")
                        saved = _snapshot_file(selected)
                        if digest is not None and (
                            saved is None
                            or hashlib.sha256(saved.content).hexdigest()
                            != digest
                        ):
                            child.channel.sendall(b"stale\n")
                            continue
                        progress = applying.pop(role, None)
                        if saved is not None:
                            good_files[role] = saved
                        else:
                            good_files.pop(role, None)
                        child.status = (
                            "recovered"
                            if progress and progress.recovering
                            else "healthy"
                        )
                        child.channel.sendall(b"accepted\n")
                        continue
                    if message != b"restart" and not message.startswith(
                        b"restart:"
                    ):
                        raise RuntimeError("unsupported Parent IPC request")
                    if role in applying:
                        child.channel.sendall(b"busy\n")
                        continue
                    if shared_config(role):
                        # Two role snapshots for one file cannot be rolled back
                        # independently without discarding the sibling's edits.
                        child.channel.sendall(b"shared\n")
                        continue
                    previous = good_files.get(role)
                    candidate = (
                        _snapshot_file(str(previous.path)) if previous else None
                    )
                    if message.startswith(b"restart:"):
                        revision = message.removeprefix(b"restart:").decode(
                            "ascii"
                        )
                        if (
                            candidate is None
                            or hashlib.sha256(candidate.content).hexdigest()
                            != revision
                        ):
                            child.channel.sendall(b"stale\n")
                            continue
                    applying[role] = _Applying(
                        candidate=None
                        if candidate is None
                        else candidate.content,
                        good=previous,
                        started_at=time.monotonic(),
                    )
                    child.channel.sendall(b"accepted\n")
                    _stop([child])
                    retire(child)
                    spawn(child.command, status="applying")
        except KeyboardInterrupt:
            return 130
        finally:
            # A repeated stop signal must not abort Child reaping midway.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                _stop(list(children.values()))
                for child in children.values():
                    if child.channel.fileno() != -1:
                        with suppress(KeyError):
                            selector.unregister(child.channel)
                        child.channel.close()
            finally:
                signal.signal(signal.SIGTERM, previous_sigterm)
                signal.signal(signal.SIGINT, previous_sigint)


def run_parent(commands: list[ChildCommand]) -> None:
    """CLI exit status mirrors the child, never a second runtime adapter."""
    raise SystemExit(supervise(commands))
