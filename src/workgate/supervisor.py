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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PARENT_FD_ENV = "WORKGATE_PARENT_IPC_FD"
_STOP_TIMEOUT_SECONDS = 3
_APPLY_TIMEOUT_SECONDS = 20
_CHANNEL_LOCK = threading.Lock()
_WINDOWS_CHILD_JOB: Any = None


def _borrow_child_socket(fd: int) -> socket.socket:
    """Duplicate the inherited socket without closing the child's original."""
    borrowed = socket.socket(fileno=fd)
    try:
        return borrowed.dup()
    finally:
        borrowed.detach()


def child_command(*args: str) -> list[str]:
    """Relaunch the same installed CLI, including frozen installations."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "workgate.main", *args]


def is_child() -> bool:
    """An environment marker alone cannot accidentally bypass supervision."""
    try:
        fd = int(os.environ[PARENT_FD_ENV])
        if os.name == "nt":
            with _borrow_child_socket(fd) as channel:
                return (
                    channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
                    == socket.SOCK_STREAM
                )
        return stat.S_ISSOCK(os.fstat(fd).st_mode)
    except KeyError, ValueError, OSError:
        return False


def supervise_cli_role(role: str, args: argparse.Namespace) -> bool:
    """Run a root CLI role under Parent, never nesting inside a Child.

    Direct CLI handler calls without root argv remain usable for tests.
    """
    arguments = getattr(args, "_workgate_argv", None)
    if arguments is None:
        return False
    if is_child():
        if os.name == "nt":
            # Each role owns its descendants across Parent-requested restarts.
            global _WINDOWS_CHILD_JOB
            if _WINDOWS_CHILD_JOB is None:
                from .platform.windows_job import WindowsJob

                job = WindowsJob()
                try:
                    job.attach_current_process()
                except BaseException:
                    job.close()
                    raise
                _WINDOWS_CHILD_JOB = (
                    job  # Keep handle open for the child's life.
                )
        return False
    run_parent([ChildCommand(role, tuple(child_command(*arguments)))])
    return True


def _parent_message(message: bytes) -> bytes:
    """Serialize all queries over the single inherited Child/Parent socket."""
    fd = int(os.environ[PARENT_FD_ENV])
    channel = (
        _borrow_child_socket(fd)
        if os.name == "nt"
        else socket.socket(fileno=os.dup(fd))
    )
    with _CHANNEL_LOCK, channel:
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


def notify_config_selected(
    config_path: Path | None, file_digest: str | None
) -> None:
    """Register the file dependency at settings load, before runtime readiness."""
    if not is_child():
        return
    selected = str(config_path) if config_path is not None else None
    message = (
        json.dumps({"config": selected, "digest": file_digest}).encode() + b"\n"
    )
    if _parent_message(message) != b"accepted\n":
        raise RuntimeError("Workgate Parent did not confirm config selection")


def restart_affects_executor() -> bool:
    """Whether this Control Apply would also restart a colocated Executor."""
    if not is_child():
        return False
    response = _parent_message(b"targets\n")
    if response not in (b"self\n", b"both\n"):
        raise RuntimeError("Workgate Parent cannot determine Apply targets")
    return response == b"both\n"


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
    if status not in {"starting", "healthy", "applying", "recovered"}:
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
    roles: tuple[str, ...]
    ready: set[str] = field(default_factory=set)
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
    config_path: Path | None = None
    registered: bool = False
    config_digest: str | None = None


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
    good_files: dict[Path, _GoodFile] = {}
    applying: _Applying | None = None
    if os.name == "nt":
        from .platform.windows_job import WindowsJob

        parent_job = WindowsJob()
        try:
            parent_job.attach_current_process()
        except BaseException:
            parent_job.close()
            raise
        # This owner handle closes when Parent exits, including an abrupt crash.
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)

    def terminate(_signal: int, _frame: Any) -> None:
        raise SystemExit(143)

    with selectors.DefaultSelector() as selector:

        def spawn(command: ChildCommand, *, status: str = "starting") -> None:
            parent_sock, child_sock = socket.socketpair()
            try:
                kwargs: dict[str, Any]
                if os.name == "nt":
                    child_sock.set_inheritable(True)
                    startup = subprocess.STARTUPINFO()
                    startup.lpAttributeList = {
                        "handle_list": [child_sock.fileno()]
                    }
                    kwargs = {
                        "startupinfo": startup,
                        "close_fds": True,
                        "creationflags": subprocess.CREATE_NO_WINDOW,
                    }
                else:
                    kwargs = {"pass_fds": (child_sock.fileno(),)}
                process = subprocess.Popen[bytes](
                    command.argv,
                    env={
                        **environment,
                        PARENT_FD_ENV: str(child_sock.fileno()),
                    },
                    **kwargs,
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

        def restart_roles(roles: tuple[str, ...]) -> None:
            old = [children[role] for role in roles]
            _stop(old)
            for child in old:
                retire(child)
            for child in old:
                spawn(child.command, status="applying")

        def record_healthy_files() -> None:
            """Record a file only once all of its dependent roles are healthy."""
            if not all(child.registered for child in children.values()):
                return
            paths = {
                child.config_path
                for child in children.values()
                if child.config_path is not None
            }
            for path in paths:
                peers = [
                    child
                    for child in children.values()
                    if child.config_path == path
                ]
                if not all(
                    child.status in {"healthy", "recovered"} for child in peers
                ):
                    continue
                snapshot = _snapshot_file(str(path))
                if snapshot is None:
                    continue
                digest = hashlib.sha256(snapshot.content).hexdigest()
                if all(child.config_digest == digest for child in peers):
                    good_files[path] = snapshot

        def recover() -> bool:
            nonlocal applying
            progress = applying
            if progress is None or progress.recovering:
                return False
            if not _restore_good(progress.good, progress.candidate):
                return False
            progress.recovering = True
            progress.ready.clear()
            restart_roles(progress.roles)
            progress.started_at = time.monotonic()
            return True

        signal.signal(signal.SIGTERM, terminate)
        try:
            for command in commands:
                spawn(command)
            while True:
                for role, child in tuple(children.items()):
                    if children[role] is not child:
                        continue  # A sibling's shared-file Apply replaced it.
                    exit_code = child.process.poll()
                    if exit_code is not None:
                        if (
                            applying is not None
                            and role in applying.roles
                            and recover()
                        ):
                            continue
                        return 128 - exit_code if exit_code < 0 else exit_code
                if (
                    applying is not None
                    and time.monotonic() - applying.started_at
                    > _APPLY_TIMEOUT_SECONDS
                ) and not recover():
                    return 1
                # Windows select() rejects an empty socket set. A role may
                # close IPC just before its process exit is observable.
                if not selector.get_map():
                    time.sleep(0.2)
                    continue
                for key, _events in selector.select(timeout=0.2):
                    role: str = key.data
                    child = children[role]
                    if key.fileobj is not child.channel:
                        continue  # Ignore socket readiness from a retired Child.
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
                    if message in {b"status", b"targets"}:
                        # Dependency information is only complete after each
                        # Child has reported its startup config selection.
                        known = all(c.registered for c in children.values())
                        group = (
                            tuple(
                                member
                                for member, peer in children.items()
                                if peer.config_path == child.config_path
                            )
                            if child.config_path is not None
                            else (role,)
                        )
                        ready = known and all(
                            children[member].status in {"healthy", "recovered"}
                            for member in group
                        )
                        if not ready:
                            result = (
                                "busy" if message == b"targets" else "starting"
                            )
                        elif message == b"targets":
                            result = (
                                "both"
                                if role == "control" and "executor" in group
                                else "self"
                            )
                        else:
                            result = child.status
                        child.channel.sendall(result.encode("ascii") + b"\n")
                        continue
                    if message.startswith(b'{"config": '):
                        payload = json.loads(message)
                        selected = payload["config"]
                        if selected is not None and not isinstance(
                            selected, str
                        ):
                            raise RuntimeError("invalid config path")
                        if child.registered:
                            child.channel.sendall(b"stale\n")
                            continue
                        child.config_path = Path(selected) if selected else None
                        child.config_digest = payload["digest"]
                        child.registered = True
                        record_healthy_files()
                        child.channel.sendall(b"accepted\n")
                        continue
                    if message.startswith(b'{"ready": '):
                        payload = json.loads(message)
                        selected = payload["ready"]
                        digest = payload["digest"]
                        if selected is not None and not isinstance(
                            selected, str
                        ):
                            raise RuntimeError("invalid ready path")
                        path = Path(selected) if selected else None
                        if not child.registered or path != child.config_path:
                            child.channel.sendall(b"stale\n")
                            continue
                        saved = _snapshot_file(selected)
                        if digest is not None and (
                            saved is None
                            or hashlib.sha256(saved.content).hexdigest()
                            != digest
                        ):
                            child.channel.sendall(b"stale\n")
                            continue
                        progress = applying
                        if progress is not None and role in progress.roles:
                            expected = (
                                progress.good.content
                                if progress.recovering and progress.good
                                else progress.candidate
                            )
                            if progress.good is not None and (
                                saved is None
                                or saved.path != progress.good.path
                                or saved.content != expected
                            ):
                                child.channel.sendall(b"stale\n")
                                continue
                            progress.ready.add(role)
                            if len(progress.ready) == len(progress.roles):
                                for member in progress.roles:
                                    children[member].status = (
                                        "recovered"
                                        if progress.recovering
                                        else "healthy"
                                    )
                                if saved is not None and all(
                                    peer.config_digest == digest
                                    for peer in children.values()
                                    if peer.config_path == saved.path
                                ):
                                    good_files[saved.path] = saved
                                applying = None
                        else:
                            child.status = "healthy"
                            record_healthy_files()
                        child.channel.sendall(b"accepted\n")
                        continue
                    if message != b"restart" and not message.startswith(
                        b"restart:"
                    ):
                        raise RuntimeError("unsupported Parent IPC request")
                    if applying is not None:
                        child.channel.sendall(b"busy\n")
                        continue
                    if message == b"restart":
                        child.channel.sendall(b"accepted\n")
                        _stop([child])
                        retire(child)
                        spawn(child.command)
                        continue
                    roles = (role,)
                    previous = None
                    if message.startswith(b"restart:"):
                        if not all(c.registered for c in children.values()):
                            child.channel.sendall(b"busy\n")
                            continue
                        path = child.config_path
                        if path is None:
                            child.channel.sendall(b"stale\n")
                            continue
                        roles = tuple(
                            member
                            for member, peer in children.items()
                            if peer.config_path == path
                        )
                        if any(
                            children[member].status
                            not in {"healthy", "recovered"}
                            for member in roles
                        ):
                            child.channel.sendall(b"busy\n")
                            continue
                        previous = good_files.get(path)
                        if previous is None:
                            child.channel.sendall(b"stale\n")
                            continue
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
                    applying = _Applying(
                        candidate=None
                        if candidate is None
                        else candidate.content,
                        good=previous,
                        started_at=time.monotonic(),
                        roles=roles,
                    )
                    child.channel.sendall(b"accepted\n")
                    restart_roles(roles)
                    applying.started_at = time.monotonic()
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
