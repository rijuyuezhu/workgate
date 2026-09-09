"""Internal POSIX runner that reaps descendants of one bounded command."""

import argparse
import contextlib
import ctypes
import json
import os
import plistlib
import select
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import FrameType
from typing import Any

PR_SET_CHILD_SUBREAPER = 36
POLL_INTERVAL_S = 0.02
TERMINATE_GRACE_S = 1.0
KILL_GRACE_S = 1.0
KQUEUE_EVENT_BATCH = 256
PROCFS_ROOT = Path("/proc")
LAUNCHCTL_PATH = Path("/bin/launchctl")
LAUNCHD_START_TIMEOUT_S = 5.0
LAUNCHD_CONTROL_TIMEOUT_S = 1.0
LAUNCHD_CLEANUP_GRACE_S = 0.25
MACOS_TERMINATE_GRACE_S = 0.5
MACOS_KILL_GRACE_S = 0.5
MACOS_LIBPROC_PATH = "/usr/lib/libproc.dylib"
PROC_UID_ONLY = 4
PROC_PIDCOALITIONINFO = 20
COALITION_NUM_TYPES = 2
COALITION_TYPE_RESOURCE = 0
_MACOS_LIBPROC: Any | None = None
_MACOS_LIBPROC_LOADED = False


def bounded_runner_argv(
    shell: str,
    command: str,
    *extra_arguments: str,
    frozen: bool | None = None,
) -> list[str]:
    """Build one trusted invocation of the private bounded-runner CLI."""
    arguments = [
        "--shell",
        shell,
        "--command",
        command,
        *extra_arguments,
    ]
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        return [sys.executable, "bounded-runner", *arguments]
    return [sys.executable, os.path.abspath(__file__), *arguments]


class _ProcPidCoalitionInfo(ctypes.Structure):
    """Public XNU proc_pidcoalitioninfo layout for resource/jetsam ids."""

    _fields_ = [
        ("coalition_id", ctypes.c_uint64 * COALITION_NUM_TYPES),
        ("reserved1", ctypes.c_uint64),
        ("reserved2", ctypes.c_uint64),
        ("reserved3", ctypes.c_uint64),
    ]


def _shell_command_args(shell: str, command: str) -> list[str]:
    """Build native argv without importing the parent shell module."""
    name = os.path.basename(shell).lower()
    if name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return [shell, "-NoProfile", "-NonInteractive", "-Command", command]
    if name in {"cmd", "cmd.exe"}:
        return [shell, "/D", "/S", "/C", command]
    return [shell, "-lc", command]


def _enable_child_subreaper() -> bool:
    """Make orphaned command descendants reparent to this runner on Linux."""
    if sys.platform != "linux":
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    except AttributeError, OSError:
        return False
    return result == 0


def _direct_children(pid: int) -> list[int] | None:
    """Return Linux procfs child pids, or None when unavailable."""
    path = PROCFS_ROOT / str(pid) / "task" / str(pid) / "children"
    try:
        raw = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        try:
            (PROCFS_ROOT / str(pid)).stat()
        except FileNotFoundError:
            # The process was reaped after its parent listed the pid.
            return []
        except OSError:
            return None
        # The process is still visible but this optional procfs entry is absent.
        return None
    except OSError, UnicodeError:
        return None
    values = raw.split()
    if any(not value.isdigit() for value in values):
        return None
    return [int(value) for value in values]


def _descendants_from_children_files(pid: int) -> list[int] | None:
    """Return descendants from per-process children files when available."""
    found: list[int] = []
    pending = _direct_children(pid)
    if pending is None:
        return None
    seen: set[int] = set()
    while pending:
        child = pending.pop()
        if child in seen:
            continue
        seen.add(child)
        found.append(child)
        children = _direct_children(child)
        if children is None:
            return None
        pending.extend(children)
    return found


def _procfs_parent_map() -> dict[int, int] | None:
    """Build an authoritative pid-to-ppid map from visible procfs status files."""
    try:
        entries = tuple(PROCFS_ROOT.iterdir())
    except OSError:
        return None

    parents: dict[int, int] = {}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "status").read_text(encoding="ascii")
        except FileNotFoundError:
            # Processes can exit between listing /proc and reading status.
            continue
        except OSError, UnicodeError:
            return None

        parent_value: str | None = None
        for line in raw.splitlines():
            if line.startswith("PPid:"):
                parent_value = line.partition(":")[2].strip()
                break
        if parent_value is None or not parent_value.isdigit():
            return None
        parents[int(entry.name)] = int(parent_value)
    return parents


def _descendants_from_parent_map(
    pid: int, parents: dict[int, int]
) -> list[int]:
    """Return descendants reachable from one pid in a procfs parent map."""
    children_by_parent: dict[int, list[int]] = {}
    for child, parent in parents.items():
        children_by_parent.setdefault(parent, []).append(child)

    found: list[int] = []
    pending = list(children_by_parent.get(pid, ()))
    seen: set[int] = set()
    while pending:
        child = pending.pop()
        if child in seen:
            continue
        seen.add(child)
        found.append(child)
        pending.extend(children_by_parent.get(child, ()))
    return found


def _descendants(pid: int) -> list[int] | None:
    """Return all visible descendants, or None when procfs is uncertain."""
    descendants = _descendants_from_children_files(pid)
    if descendants is not None:
        return descendants
    parents = _procfs_parent_map()
    if parents is None:
        return None
    descendants = _descendants_from_parent_map(pid, parents)
    if descendants:
        return descendants

    # A process can fork after the directory snapshot and exit before its
    # status is read, leaving the newly adopted child absent from this map.
    # Never accept an empty fallback result without fresh authoritative proof.
    parents = _procfs_parent_map()
    if parents is None:
        return None
    return _descendants_from_parent_map(pid, parents)


def _signal_descendants(sig: signal.Signals) -> bool:
    """Signal every known descendant, failing when enumeration is uncertain."""
    descendants = _descendants(os.getpid())
    if descendants is None:
        return False
    signalled = True
    for pid in reversed(descendants):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            continue
        except PermissionError:
            signalled = False
    return signalled


def _force_kill_signal() -> signal.Signals:
    """Return the strongest process signal available on this platform."""
    return getattr(signal, "SIGKILL", signal.SIGTERM)


def _wait_for_no_descendants(timeout_s: float) -> bool:
    """Wait briefly for all command descendants to exit."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _reap_children()
        descendants = _descendants(os.getpid())
        if descendants is None:
            return False
        if not descendants:
            return True
        time.sleep(POLL_INTERVAL_S)
    _reap_children()
    descendants = _descendants(os.getpid())
    return descendants is not None and not descendants


def _reap_children() -> None:
    """Reap any exited descendants adopted through subreaper mode."""
    waitpid = getattr(os, "waitpid", None)
    no_hang = getattr(os, "WNOHANG", None)
    if waitpid is None or no_hang is None:
        return
    while True:
        try:
            pid, _status = waitpid(-1, no_hang)
        except ChildProcessError:
            return
        if pid <= 0:
            return


def _cleanup_descendants() -> bool:
    """Terminate descendants left behind after a bounded command returns."""
    descendants = _descendants(os.getpid())
    if descendants is None:
        return False
    if not descendants:
        return True
    if not _signal_descendants(signal.SIGTERM):
        return False
    if _wait_for_no_descendants(TERMINATE_GRACE_S):
        _reap_children()
        return True
    if not _signal_descendants(_force_kill_signal()):
        return False
    cleaned = _wait_for_no_descendants(KILL_GRACE_S)
    _reap_children()
    return cleaned


def _process_group_containment_available() -> bool:
    """Return whether this host can isolate and signal a child process group."""
    return callable(getattr(os, "killpg", None))


def _kqueue_descendant_tracking_available() -> bool:
    """Return whether this host exposes kernel-tracked fork descendants."""
    if sys.platform == "darwin":
        # XNU has rejected NOTE_TRACK, NOTE_TRACKERR, and NOTE_CHILD since 10.5.
        return False
    required = (
        "kqueue",
        "kevent",
        "KQ_FILTER_PROC",
        "KQ_EV_ADD",
        "KQ_EV_ENABLE",
        "KQ_NOTE_EXIT",
        "KQ_NOTE_FORK",
        "KQ_NOTE_TRACK",
        "KQ_NOTE_CHILD",
        "KQ_NOTE_TRACKERR",
    )
    return (
        all(hasattr(select, name) for name in required)
        and callable(getattr(os, "fork", None))
        and callable(getattr(os, "waitpid", None))
    )


def _launchd_containment_available() -> bool:
    """Return whether macOS launchd can host an isolated bounded job."""
    return (
        sys.platform == "darwin"
        and LAUNCHCTL_PATH.is_file()
        and os.access(LAUNCHCTL_PATH, os.X_OK)
        and _macos_process_coalition_ids(os.getpid()) is not None
        and _macos_uid_pids() is not None
    )


def _macos_libproc() -> Any | None:
    """Load the public macOS libproc process-enumeration interface once."""
    global _MACOS_LIBPROC, _MACOS_LIBPROC_LOADED
    if _MACOS_LIBPROC_LOADED:
        return _MACOS_LIBPROC
    _MACOS_LIBPROC_LOADED = True
    if sys.platform != "darwin":
        return None
    try:
        library = ctypes.CDLL(MACOS_LIBPROC_PATH, use_errno=True)
    except OSError:
        return None
    library.proc_listpids.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    library.proc_listpids.restype = ctypes.c_int
    library.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    library.proc_pidinfo.restype = ctypes.c_int
    _MACOS_LIBPROC = library
    return library


def _macos_uid_pids() -> list[int] | None:
    """Enumerate every process owned by this uid through public libproc."""
    library = _macos_libproc()
    if library is None:
        return None
    required_bytes = int(
        library.proc_listpids(PROC_UID_ONLY, os.getuid(), None, 0)
    )
    if required_bytes <= 0:
        return None
    capacity = max(required_bytes // ctypes.sizeof(ctypes.c_int) + 64, 128)
    for _ in range(3):
        buffer = (ctypes.c_int * capacity)()
        returned_bytes = int(
            library.proc_listpids(
                PROC_UID_ONLY,
                os.getuid(),
                buffer,
                ctypes.sizeof(buffer),
            )
        )
        if returned_bytes <= 0:
            return None
        count = returned_bytes // ctypes.sizeof(ctypes.c_int)
        if count < capacity:
            return [int(pid) for pid in buffer[:count] if int(pid) > 0]
        capacity *= 2
    return None


def _macos_process_coalition_ids(pid: int) -> tuple[int, int] | None:
    """Return one process's public resource and jetsam coalition ids."""
    library = _macos_libproc()
    if library is None:
        return None
    info = _ProcPidCoalitionInfo()
    size = ctypes.sizeof(info)
    returned = int(
        library.proc_pidinfo(
            int(pid),
            PROC_PIDCOALITIONINFO,
            0,
            ctypes.byref(info),
            size,
        )
    )
    if returned != size:
        return None
    resource = int(info.coalition_id[COALITION_TYPE_RESOURCE])
    jetsam = int(info.coalition_id[1])
    if resource <= 0:
        return None
    return resource, jetsam


def _read_launchd_coalition(path: Path) -> tuple[int, int] | None:
    """Read and validate the launchd child's kernel coalition identity."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError, UnicodeError, json.JSONDecodeError:
        return (0, 0)
    raw = payload.get("coalition_ids") if isinstance(payload, dict) else None
    if (
        not isinstance(raw, list)
        or len(raw) != COALITION_NUM_TYPES
        or any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in raw
        )
        or int(raw[COALITION_TYPE_RESOURCE]) <= 0
    ):
        return (0, 0)
    return int(raw[0]), int(raw[1])


def _macos_matching_coalition_pids(
    coalition_ids: tuple[int, int],
) -> set[int] | None:
    """Return all current-uid processes in one resource coalition."""
    pids = _macos_uid_pids()
    if pids is None:
        return None
    resource_id = coalition_ids[COALITION_TYPE_RESOURCE]
    matching: set[int] = set()
    for pid in pids:
        if pid == os.getpid():
            continue
        identity = _macos_process_coalition_ids(pid)
        if identity is None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                return None
            return None
        if identity[COALITION_TYPE_RESOURCE] == resource_id:
            matching.add(pid)
    return matching


def _terminate_macos_coalition(
    coalition_ids: tuple[int, int],
    sig: signal.Signals,
    timeout_s: float,
) -> bool:
    """Repeatedly signal a coalition until it is empty or the deadline expires."""
    deadline = time.monotonic() + timeout_s
    while True:
        matching = _macos_matching_coalition_pids(coalition_ids)
        if matching is None:
            return False
        if not matching:
            return True
        for pid in sorted(matching, reverse=True):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                continue
            except PermissionError:
                return False
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL_INTERVAL_S)
    matching = _macos_matching_coalition_pids(coalition_ids)
    return matching == set()


def _cleanup_macos_coalition(coalition_ids: tuple[int, int]) -> bool:
    """Terminate every surviving launchd-job process, including setsid escapes."""
    if _terminate_macos_coalition(
        coalition_ids, signal.SIGTERM, MACOS_TERMINATE_GRACE_S
    ):
        return True
    return _terminate_macos_coalition(
        coalition_ids, signal.SIGKILL, MACOS_KILL_GRACE_S
    )


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write one owner-private launchd child status object."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def _run_launchd_child(
    shell: str,
    command: str,
    *,
    status_path: Path,
    started_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    coalition_path: Path | None = None,
    release_path: Path | None = None,
) -> int:
    """Run the command inside one launchd job and publish its real status."""
    stdout_fd = os.open(stdout_path, os.O_WRONLY)
    stderr_fd = os.open(stderr_path, os.O_WRONLY)
    returncode = 125
    try:
        try:
            if (coalition_path is None) != (release_path is None):
                raise RuntimeError("incomplete launchd coalition gate")
            if coalition_path is not None and release_path is not None:
                coalition_ids = _macos_process_coalition_ids(os.getpid())
                if coalition_ids is None:
                    raise RuntimeError(
                        "launchd child coalition identity is unavailable"
                    )
                _write_private_json(
                    coalition_path,
                    {"coalition_ids": list(coalition_ids)},
                )
            started_path.write_text("started", encoding="ascii")
            started_path.chmod(0o600)
            if release_path is not None:
                deadline = time.monotonic() + LAUNCHD_START_TIMEOUT_S
                while not release_path.exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "launchd child was not released by its parent"
                        )
                    time.sleep(POLL_INTERVAL_S)
            process = subprocess.Popen(
                _shell_command_args(shell, command),
                stdout=stdout_fd,
                stderr=stderr_fd,
                start_new_session=True,
            )
            returncode = int(process.wait())
        except FileNotFoundError as exc:
            os.write(
                stderr_fd,
                f"Unable to start configured shell {shell!r}: {exc}\n".encode(),
            )
            returncode = 127
        except BaseException as exc:
            os.write(
                stderr_fd,
                f"Unable to run launchd-contained command: {exc}\n".encode(),
            )
            returncode = 125
        _write_private_json(status_path, {"returncode": returncode})
        # Prevent launchd from restarting a one-shot command whose real status
        # was non-zero; the parent reads the durable status object instead.
        return 0
    finally:
        os.close(stdout_fd)
        os.close(stderr_fd)


def _drain_launchd_pipe(descriptor: int, stream: Any) -> bool:
    """Forward every currently available FIFO byte to the runner stream."""
    drained = False
    while True:
        try:
            chunk = os.read(descriptor, 64 * 1024)
        except BlockingIOError:
            break
        if not chunk:
            break
        drained = True
        binary = getattr(stream, "buffer", None)
        if binary is not None:
            binary.write(chunk)
            binary.flush()
        else:
            stream.write(chunk.decode("utf-8", errors="replace"))
            stream.flush()
    return drained


def _launchd_domain_targets() -> tuple[str, ...]:
    """Return launchd domains usable by the current unprivileged user."""
    uid = os.getuid()
    return (f"gui/{uid}", f"user/{uid}")


def _launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one bounded launchctl control operation."""
    return subprocess.run(
        [str(LAUNCHCTL_PATH), *args],
        capture_output=True,
        text=True,
        timeout=LAUNCHD_CONTROL_TIMEOUT_S,
        check=False,
    )


def _bootout_launchd_job(target: str) -> bool:
    """Remove one launchd service; coalition members are cleaned separately."""
    try:
        result = _launchctl(["bootout", target])
    except OSError, subprocess.TimeoutExpired:
        return False
    if result.returncode != 0:
        return False
    time.sleep(LAUNCHD_CLEANUP_GRACE_S)
    return True


def _read_launchd_status(path: Path) -> int | None:
    """Return a validated child status, None while it is not published."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError, UnicodeError, json.JSONDecodeError:
        return 125
    value = payload.get("returncode") if isinstance(payload, dict) else None
    if not isinstance(value, int) or isinstance(value, bool):
        return 125
    return value


def _run_launchd_bounded_command(shell: str, command: str) -> int:
    """Run one macOS command in a disposable launchd containment job."""
    parent_coalition = _macos_process_coalition_ids(os.getpid())
    if parent_coalition is None:
        print(
            "Bounded command coalition identity is unavailable",
            file=sys.stderr,
        )
        return 125
    received_signal = 0

    def remember_signal(signum: int, _frame: FrameType | None) -> None:
        nonlocal received_signal
        received_signal = received_signal or signum

    for handled in _handled_signals():
        signal.signal(handled, remember_signal)

    with tempfile.TemporaryDirectory(prefix="workgate-bounded-") as raw_dir:
        directory = Path(raw_dir)
        directory.chmod(0o700)
        status_path = directory / "status.json"
        started_path = directory / "started"
        coalition_path = directory / "coalition.json"
        release_path = directory / "release"
        stdout_path = directory / "stdout.fifo"
        stderr_path = directory / "stderr.fifo"
        plist_path = directory / "job.plist"
        os.mkfifo(stdout_path, 0o600)
        os.mkfifo(stderr_path, 0o600)
        stdout_fd = os.open(stdout_path, os.O_RDONLY | os.O_NONBLOCK)
        stderr_fd = os.open(stderr_path, os.O_RDONLY | os.O_NONBLOCK)
        stdout_guard = os.open(stdout_path, os.O_WRONLY | os.O_NONBLOCK)
        stderr_guard = os.open(stderr_path, os.O_WRONLY | os.O_NONBLOCK)
        label = f"workgate.bounded.{os.getpid()}.{uuid.uuid4().hex}"
        payload = {
            "Label": label,
            "ProgramArguments": bounded_runner_argv(
                shell,
                command,
                "--launchd-child-status",
                str(status_path),
                "--launchd-child-started",
                str(started_path),
                "--launchd-child-stdout",
                str(stdout_path),
                "--launchd-child-stderr",
                str(stderr_path),
                "--launchd-child-coalition",
                str(coalition_path),
                "--launchd-child-release",
                str(release_path),
            ),
            "WorkingDirectory": os.getcwd(),
            "EnvironmentVariables": dict(os.environ),
            "RunAtLoad": True,
            "KeepAlive": False,
            "AbandonProcessGroup": False,
            "ProcessType": "Interactive",
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": "/dev/null",
        }
        with plist_path.open("wb") as handle:
            plistlib.dump(payload, handle, fmt=plistlib.FMT_XML, sort_keys=True)
        plist_path.chmod(0o600)

        target: str | None = None
        uncertain_target: str | None = None
        bootout_cleaned = False
        coalition_cleaned = False
        coalition_ids: tuple[int, int] | None = None
        released = False
        bootstrap_errors: list[str] = []
        try:
            for domain in dict.fromkeys(_launchd_domain_targets()):
                try:
                    result = _launchctl(["bootstrap", domain, str(plist_path)])
                except subprocess.TimeoutExpired as exc:
                    bootstrap_errors.append(str(exc))
                    candidate = f"{domain}/{label}"
                    if not _bootout_launchd_job(candidate):
                        uncertain_target = candidate
                    break
                except OSError as exc:
                    bootstrap_errors.append(str(exc))
                    continue
                if result.returncode == 0:
                    target = f"{domain}/{label}"
                    break
                bootstrap_errors.append(
                    (
                        result.stderr
                        or result.stdout
                        or "unknown launchctl error"
                    ).strip()
                )
            if target is None:
                print(
                    "Bounded command launchd registration failed: "
                    + "; ".join(error for error in bootstrap_errors if error),
                    file=sys.stderr,
                )
                return 125

            started_deadline = time.monotonic() + LAUNCHD_START_TIMEOUT_S
            returncode: int | None = None
            startup_failed = False
            while returncode is None and not received_signal:
                _drain_launchd_pipe(stdout_fd, sys.stdout)
                _drain_launchd_pipe(stderr_fd, sys.stderr)
                returncode = _read_launchd_status(status_path)
                if coalition_ids is None:
                    candidate = _read_launchd_coalition(coalition_path)
                    if candidate == (0, 0):
                        startup_failed = True
                        break
                    if candidate is not None:
                        coalition_ids = candidate
                        if (
                            coalition_ids[COALITION_TYPE_RESOURCE]
                            == parent_coalition[COALITION_TYPE_RESOURCE]
                        ):
                            startup_failed = True
                            break
                        _write_private_json(release_path, {"release": True})
                        released = True
                if (
                    returncode is None
                    and (
                        not started_path.exists()
                        or coalition_ids is None
                        or not released
                    )
                    and time.monotonic() >= started_deadline
                ):
                    startup_failed = True
                    break
                time.sleep(POLL_INTERVAL_S)

            bootout_cleaned = _bootout_launchd_job(target)
            coalition_cleaned = not released
            if coalition_ids is not None and released:
                coalition_cleaned = _cleanup_macos_coalition(coalition_ids)
            for _ in range(4):
                stdout_drained = _drain_launchd_pipe(stdout_fd, sys.stdout)
                stderr_drained = _drain_launchd_pipe(stderr_fd, sys.stderr)
                if not stdout_drained and not stderr_drained:
                    break
                time.sleep(POLL_INTERVAL_S)
            if not bootout_cleaned or not coalition_cleaned:
                print(
                    "Bounded command launchd containment cleanup failed",
                    file=sys.stderr,
                )
                return 125
            if startup_failed:
                print(
                    "Bounded command launchd child did not start",
                    file=sys.stderr,
                )
                return 125
            if received_signal:
                return _mirror_signal(received_signal)
            if returncode is None:
                return 125
            if returncode < 0:
                return _mirror_signal(-returncode)
            return returncode
        finally:
            if target is not None and not bootout_cleaned:
                _bootout_launchd_job(target)
            elif uncertain_target is not None:
                _bootout_launchd_job(uncertain_target)
            if coalition_ids is not None and released and not coalition_cleaned:
                _cleanup_macos_coalition(coalition_ids)
            os.close(stdout_guard)
            os.close(stderr_guard)
            os.close(stdout_fd)
            os.close(stderr_fd)


def _select_capability(name: str) -> Any:
    """Return one previously validated platform-specific select capability."""
    value = getattr(select, name, None)
    if value is None:
        raise RuntimeError(f"select capability {name} is unavailable")
    return value


class _KqueueDescendantTracker:
    """Track a process and all fork descendants through EVFILT_PROC."""

    def __init__(self, queue: Any, root_pid: int) -> None:
        self._queue = queue
        self.root_pid = root_pid
        self.live_pids = {root_pid}
        self.failed = False

    @classmethod
    def create(cls, root_pid: int) -> _KqueueDescendantTracker:
        """Register one gated root before it can fork."""
        queue = _select_capability("kqueue")()
        try:
            event = _select_capability("kevent")(
                root_pid,
                filter=_select_capability("KQ_FILTER_PROC"),
                flags=(
                    _select_capability("KQ_EV_ADD")
                    | _select_capability("KQ_EV_ENABLE")
                ),
                fflags=(
                    _select_capability("KQ_NOTE_EXIT")
                    | _select_capability("KQ_NOTE_FORK")
                    | _select_capability("KQ_NOTE_TRACK")
                ),
            )
            queue.control([event], 0, 0)
        except BaseException:
            queue.close()
            raise
        return cls(queue, root_pid)

    def poll(self) -> None:
        """Drain pending process events and update the live descendant set."""
        while True:
            try:
                events = self._queue.control(None, KQUEUE_EVENT_BATCH, 0)
            except OSError:
                self.failed = True
                return
            if not events:
                return
            for event in events:
                pid = int(event.ident)
                flags = int(event.fflags)
                if flags & _select_capability("KQ_NOTE_TRACKERR"):
                    self.failed = True
                if flags & _select_capability("KQ_NOTE_CHILD"):
                    self.live_pids.add(pid)
                if flags & _select_capability("KQ_NOTE_EXIT"):
                    self.live_pids.discard(pid)

    def discard_root(self) -> None:
        """Forget the root after subprocess.wait() has confirmed its exit."""
        self.live_pids.discard(self.root_pid)

    def signal_all(self, sig: signal.Signals) -> None:
        """Signal every currently tracked process, including escaped groups."""
        self.poll()
        for pid in sorted(self.live_pids, reverse=True):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                self.live_pids.discard(pid)
            except PermissionError:
                self.failed = True

    def wait_for_empty(self, timeout_s: float) -> bool:
        """Wait until every tracked process has emitted NOTE_EXIT."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.poll()
            if self.failed:
                return False
            if not self.live_pids:
                return True
            time.sleep(POLL_INTERVAL_S)
        self.poll()
        return not self.failed and not self.live_pids

    def cleanup(self) -> bool:
        """Terminate all tracked processes, escalating after a grace period."""
        self.poll()
        tracking_failed = self.failed
        if not self.live_pids:
            return not tracking_failed
        self.signal_all(signal.SIGTERM)
        if self.wait_for_empty(TERMINATE_GRACE_S):
            return not tracking_failed
        self.signal_all(_force_kill_signal())
        return self.wait_for_empty(KILL_GRACE_S) and not tracking_failed

    def close(self) -> None:
        """Release the kernel event queue."""
        self._queue.close()


def _process_group_alive(process_group_id: int) -> bool:
    """Return whether at least one process remains in the child process group."""
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_process_group(process_group_id: int, sig: signal.Signals) -> None:
    """Signal one isolated child process group while tolerating exit races."""
    try:
        os.killpg(process_group_id, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        return


def _wait_for_process_group_exit(
    process_group_id: int, timeout_s: float
) -> bool:
    """Wait briefly for an isolated child process group to disappear."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _reap_children()
        if not _process_group_alive(process_group_id):
            return True
        time.sleep(POLL_INTERVAL_S)
    _reap_children()
    return not _process_group_alive(process_group_id)


def _cleanup_process_group(process_group_id: int) -> bool:
    """Terminate a child process group, escalating after a grace period."""
    if not _process_group_alive(process_group_id):
        return True
    _signal_process_group(process_group_id, signal.SIGTERM)
    if _wait_for_process_group_exit(process_group_id, TERMINATE_GRACE_S):
        return True
    _signal_process_group(process_group_id, _force_kill_signal())
    return _wait_for_process_group_exit(process_group_id, KILL_GRACE_S)


def _cleanup_command_processes(
    process_group_id: int | None, *, track_descendants: bool
) -> bool:
    """Clean the isolated process group and any subreaper-adopted escapees."""
    group_clean = (
        True
        if process_group_id is None
        else _cleanup_process_group(process_group_id)
    )
    descendant_clean = _cleanup_descendants() if track_descendants else True
    if not group_clean and process_group_id is not None:
        group_clean = _wait_for_process_group_exit(
            process_group_id, POLL_INTERVAL_S * 5
        )
    return group_clean and descendant_clean


def _cleanup_kqueue_command_processes(
    process_group_id: int | None,
    tracker: _KqueueDescendantTracker,
    *,
    root_exited: bool,
) -> bool:
    """Clean a process group plus kqueue-tracked session escapees."""
    if root_exited:
        tracker.discard_root()
    group_clean = (
        True
        if process_group_id is None
        else _cleanup_process_group(process_group_id)
    )
    descendant_clean = tracker.cleanup()
    if not group_clean and process_group_id is not None:
        group_clean = _wait_for_process_group_exit(
            process_group_id, POLL_INTERVAL_S * 5
        )
    return group_clean and descendant_clean


def _run_bounded_exec_gate(gate_fd: int, shell: str, command: str) -> int:
    """Wait for parent containment registration, then replace this process."""
    try:
        released = os.read(gate_fd, 1)
    except OSError as exc:
        print(
            f"Unable to read bounded command exec gate: {exc}", file=sys.stderr
        )
        return 125
    finally:
        with contextlib.suppress(OSError):
            os.close(gate_fd)
    if released != b"\x01":
        print(
            "Bounded command containment registration failed", file=sys.stderr
        )
        return 125
    try:
        os.execvp(shell, _shell_command_args(shell, command))
    except OSError as exc:
        print(
            f"Unable to start configured shell {shell!r}: {exc}",
            file=sys.stderr,
        )
        return 127


class _ForkedProcess:
    """Minimal subprocess-compatible handle for one directly forked child."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._returncode: int | None = None

    def _record_status(self, status: int) -> int:
        self._returncode = os.waitstatus_to_exitcode(status)
        return self._returncode

    def poll(self) -> int | None:
        """Return the child status without blocking."""
        if self._returncode is not None:
            return self._returncode
        try:
            waited, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            return self._returncode
        if waited == 0:
            return None
        return self._record_status(status)

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the child, with the subset of Popen semantics used here."""
        if self._returncode is not None:
            return self._returncode
        if timeout is None:
            try:
                _waited, status = os.waitpid(self.pid, 0)
            except ChildProcessError:
                self._returncode = 0
                return self._returncode
            return self._record_status(status)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            returncode = self.poll()
            if returncode is not None:
                return returncode
            time.sleep(POLL_INTERVAL_S)
        raise subprocess.TimeoutExpired("bounded command", timeout)

    def kill(self) -> None:
        """Force the forked child to exit."""
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, _force_kill_signal())


def _reset_child_signal_handlers() -> None:
    """Restore termination signals before the forked child waits or execs."""
    for handled in _handled_signals():
        signal.signal(handled, signal.SIG_DFL)


def _spawn_kqueue_tracked_command(
    shell: str, command: str, *, start_new_session: bool
) -> tuple[_ForkedProcess, _KqueueDescendantTracker] | None:
    """Spawn a gated command and register fork tracking before release."""
    gate_read, gate_write = os.pipe()
    try:
        pid = int(os.fork())
    except BaseException:
        os.close(gate_read)
        os.close(gate_write)
        raise
    if pid == 0:
        exit_code = 125
        try:
            os.close(gate_write)
            _reset_child_signal_handlers()
            if start_new_session:
                os.setsid()
            exit_code = _run_bounded_exec_gate(gate_read, shell, command)
        except BaseException as exc:
            print(
                f"Unable to prepare bounded command child: {exc}",
                file=sys.stderr,
            )
        finally:
            os._exit(exit_code)
    process = _ForkedProcess(pid)
    try:
        os.close(gate_read)
    except BaseException:
        os.close(gate_write)
        process.kill()
        process.wait()
        raise
    try:
        tracker = _KqueueDescendantTracker.create(process.pid)
    except BaseException:
        os.close(gate_write)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=TERMINATE_GRACE_S)
        if process.poll() is None:
            process.kill()
            process.wait()
        return None
    try:
        os.write(gate_write, b"\x01")
    except OSError:
        os.close(gate_write)
        tracker.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=TERMINATE_GRACE_S)
        if process.poll() is None:
            process.kill()
            process.wait()
        return None
    finally:
        with contextlib.suppress(OSError):
            os.close(gate_write)
    return process, tracker


def _mirror_signal(signum: int) -> int:
    """Exit through the same signal so the parent observes a negative code."""
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    return 128 + signum


def _handled_signals() -> tuple[signal.Signals, ...]:
    """Return only termination signals available on the current platform."""
    return tuple(
        handled
        for name in ("SIGINT", "SIGTERM", "SIGHUP")
        if (handled := getattr(signal, name, None)) is not None
    )


def run_bounded_command(shell: str, command: str) -> int:
    """Run one command and guarantee that surviving descendants are removed."""
    if sys.platform == "darwin":
        if not _launchd_containment_available():
            print(
                "Bounded command launchd containment is unavailable",
                file=sys.stderr,
            )
            return 125
        return _run_launchd_bounded_command(shell, command)
    track_descendants = _enable_child_subreaper()
    track_with_kqueue = (
        not track_descendants and _kqueue_descendant_tracking_available()
    )
    process_group_available = _process_group_containment_available()
    if not track_descendants and not track_with_kqueue:
        print(
            "Bounded command descendant tracking is unavailable on this host",
            file=sys.stderr,
        )
        return 125
    if track_descendants and _descendants(os.getpid()) is None:
        print(
            "Bounded command procfs descendant tracking is unavailable",
            file=sys.stderr,
        )
        return 125
    received_signal = 0

    def remember_signal(signum: int, _frame: FrameType | None) -> None:
        nonlocal received_signal
        received_signal = received_signal or signum

    for handled in _handled_signals():
        signal.signal(handled, remember_signal)

    tracker: _KqueueDescendantTracker | None = None
    try:
        if track_with_kqueue:
            spawned = _spawn_kqueue_tracked_command(
                shell,
                command,
                start_new_session=process_group_available,
            )
            if spawned is None:
                print(
                    "Bounded command descendant tracking registration failed",
                    file=sys.stderr,
                )
                return 125
            process, tracker = spawned
        else:
            process = subprocess.Popen(
                _shell_command_args(shell, command),
                start_new_session=process_group_available,
            )
    except FileNotFoundError as exc:
        print(
            f"Unable to start configured shell {shell!r}: {exc}",
            file=sys.stderr,
        )
        return 127

    try:
        containment_failed = False
        while process.poll() is None and not received_signal:
            if tracker is not None:
                tracker.poll()
                if tracker.failed:
                    containment_failed = True
                    break
            time.sleep(POLL_INTERVAL_S)

        process_group_id = process.pid if process_group_available else None
        if received_signal or containment_failed:
            if tracker is None:
                cleaned = _cleanup_command_processes(
                    process_group_id,
                    track_descendants=track_descendants,
                )
            else:
                cleaned = _cleanup_kqueue_command_processes(
                    process_group_id,
                    tracker,
                    root_exited=False,
                )
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=KILL_GRACE_S)
            if containment_failed or not cleaned:
                print(
                    "Bounded command descendant tracking failed during execution",
                    file=sys.stderr,
                )
                return 125
            return _mirror_signal(received_signal)

        returncode = process.wait()
        if tracker is None:
            cleaned = _cleanup_command_processes(
                process_group_id,
                track_descendants=track_descendants,
            )
        else:
            cleaned = _cleanup_kqueue_command_processes(
                process_group_id,
                tracker,
                root_exited=True,
            )
        if not cleaned:
            print(
                "Bounded command descendants did not exit after forced termination",
                file=sys.stderr,
            )
            return 125
        if returncode < 0:
            return _mirror_signal(-returncode)
        return returncode
    finally:
        if tracker is not None:
            tracker.close()


def run_bounded_runner_from_args(args: argparse.Namespace) -> None:
    """Argparse handler for the private bounded-runner subcommand."""
    launchd_child_status = getattr(args, "launchd_child_status", None)
    if launchd_child_status is not None:
        launchd_child_started = getattr(args, "launchd_child_started", None)
        launchd_child_stdout = getattr(args, "launchd_child_stdout", None)
        launchd_child_stderr = getattr(args, "launchd_child_stderr", None)
        launchd_child_coalition = getattr(args, "launchd_child_coalition", None)
        launchd_child_release = getattr(args, "launchd_child_release", None)
        if (
            not isinstance(launchd_child_status, str)
            or not launchd_child_status
            or not isinstance(launchd_child_started, str)
            or not launchd_child_started
            or not isinstance(launchd_child_stdout, str)
            or not launchd_child_stdout
            or not isinstance(launchd_child_stderr, str)
            or not launchd_child_stderr
            or not isinstance(launchd_child_coalition, str)
            or not launchd_child_coalition
            or not isinstance(launchd_child_release, str)
            or not launchd_child_release
        ):
            raise SystemExit(125)
        raise SystemExit(
            _run_launchd_child(
                str(args.shell),
                str(args.command),
                status_path=Path(launchd_child_status),
                started_path=Path(launchd_child_started),
                stdout_path=Path(launchd_child_stdout),
                stderr_path=Path(launchd_child_stderr),
                coalition_path=Path(launchd_child_coalition),
                release_path=Path(launchd_child_release),
            )
        )
    raise SystemExit(run_bounded_command(str(args.shell), str(args.command)))


def _add_launchd_child_arguments(parser: argparse.ArgumentParser) -> None:
    """Register hidden arguments used only by a launchd-contained child."""
    parser.add_argument("--launchd-child-status", help=argparse.SUPPRESS)
    parser.add_argument("--launchd-child-started", help=argparse.SUPPRESS)
    parser.add_argument("--launchd-child-stdout", help=argparse.SUPPRESS)
    parser.add_argument("--launchd-child-stderr", help=argparse.SUPPRESS)
    parser.add_argument("--launchd-child-coalition", help=argparse.SUPPRESS)
    parser.add_argument("--launchd-child-release", help=argparse.SUPPRESS)


def register_bounded_runner_cli(subparsers: Any) -> argparse.ArgumentParser:
    """Register the private bounded process runner."""
    parser = subparsers.add_parser(
        "bounded-runner",
        help="Run one bounded command and reap descendants (internal)",
        description="Run one bounded command and reap descendants. Internal interface.",
    )
    parser.add_argument("--shell", required=True)
    parser.add_argument("--command", required=True)
    _add_launchd_child_arguments(parser)
    parser.set_defaults(handler=run_bounded_runner_from_args)
    return parser


def main() -> None:
    """Run the lightweight module entry point without importing the server CLI."""
    parser = argparse.ArgumentParser(
        prog="workgate-bounded-runner",
        description="Run one bounded command and reap descendants.",
    )
    parser.add_argument("--shell", required=True)
    parser.add_argument("--command", required=True)
    _add_launchd_child_arguments(parser)
    run_bounded_runner_from_args(parser.parse_args())


if __name__ == "__main__":
    main()
