"""Native Windows Parent/Child IPC, restart and descendant cleanup acceptance."""

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if os.name != "nt":
    raise RuntimeError("Run this smoke with native Windows Python 3.14")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from workgate import supervisor  # noqa: E402


def _dead(pid: int) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        return True
    try:
        return kernel.WaitForSingleObject(handle, 5000) == 0
    finally:
        kernel.CloseHandle(handle)


def _wait_for(predicate, detail: str) -> None:
    for _ in range(100):
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {detail}")


def _role(root: Path, name: str) -> None:
    assert supervisor.is_child(), "missing private inherited Windows socket"
    assert not supervisor.supervise_cli_role(
        name, SimpleNamespace(_workgate_argv=[name])
    )
    # Nested kill-on-close Job Object belongs to this role across restarts.
    descendant = subprocess.Popen(
        [sys.executable, str(Path(__file__)), "sleep"],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (root / f"{name}.{os.getpid()}.pids").write_text(
        f"{os.getpid()} {descendant.pid}", encoding="ascii"
    )
    supervisor.notify_config_selected(None, None)
    supervisor.notify_ready(None, None)
    while True:
        if (root / f"{name}.exit").exists():
            return
        if (root / f"{name}.restart").exists():
            (root / f"{name}.restart").unlink()
            supervisor.request_restart()
        time.sleep(0.05)


def _parent(root: Path) -> None:
    assert not supervisor.is_child()
    commands = [
        supervisor.ChildCommand(
            name, (sys.executable, str(Path(__file__)), "role", str(root), name)
        )
        for name in ("control", "executor")
    ]
    raise SystemExit(supervisor.supervise(commands))


def _rows(root: Path, name: str) -> list[tuple[int, int]]:
    return [
        tuple(map(int, path.read_text(encoding="ascii").split()))
        for path in sorted(root.glob(f"{name}.*.pids"))
    ]


def _test(root: Path, *, forced: bool) -> None:
    root.mkdir()
    parent = subprocess.Popen(
        [sys.executable, str(Path(__file__)), "parent", str(root)],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    seen: list[tuple[int, int]] = []
    try:
        _wait_for(
            lambda: bool(_rows(root, "control") and _rows(root, "executor")),
            "both supervised Windows roles",
        )
        assert parent.poll() is None
        seen.extend(_rows(root, "control") + _rows(root, "executor"))
        if not forced:
            (root / "control.restart").touch()
            _wait_for(
                lambda: len(_rows(root, "control")) == 2,
                "Windows Parent-requested control restart",
            )
            new_role = next(
                row for row in _rows(root, "control") if row != seen[0]
            )
            seen.append(new_role)
            assert _dead(seen[0][0]), "old control child still alive"
            assert _dead(seen[0][1]), "old control grandchild leaked"
            assert not _dead(seen[1][0]), "unrelated executor was restarted"
            (root / "control.exit").touch()
            assert parent.wait(timeout=15) == 0
        else:
            parent.kill()
            parent.wait(timeout=10)
        for role_pid, descendant_pid in seen:
            assert _dead(role_pid), f"orphaned role PID {role_pid}"
            assert _dead(descendant_pid), (
                f"orphaned role descendant PID {descendant_pid}"
            )
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)


def run() -> None:
    assert not supervisor.is_child()
    with patch.dict(os.environ, {supervisor.PARENT_FD_ENV: "999999"}):
        assert not supervisor.is_child(), (
            "invalid inherited socket was accepted"
        )

    # Fail closed before any role launch when Job containment is unavailable.
    from workgate.platform import windows_job

    class RefusedJob:
        closed = False

        def attach_current_process(self) -> None:
            raise OSError("Job assignment refused")

        def close(self) -> None:
            self.closed = True

    refused = RefusedJob()
    with (
        patch.object(windows_job, "WindowsJob", return_value=refused),
        patch.object(
            supervisor.subprocess,
            "Popen",
            side_effect=AssertionError("uncontained child spawned"),
        ),
    ):
        try:
            supervisor.supervise(
                [supervisor.ChildCommand("control", ("unused",))]
            )
        except OSError as exc:
            assert "Job assignment refused" in str(exc)
        else:
            raise AssertionError("missing Job containment was accepted")
    assert refused.closed, "failed Job Object not closed"

    with tempfile.TemporaryDirectory(prefix="workgate-supervisor-win-") as temp:
        root = Path(temp)
        _test(root / "restart", forced=False)
        _test(root / "crash", forced=True)
    print(
        "Native Windows shared Parent/Child IPC, restart and crash cleanup: PASS"
    )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "sleep":
        time.sleep(60)
    elif len(sys.argv) > 1 and sys.argv[1] == "role":
        _role(Path(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) > 1 and sys.argv[1] == "parent":
        _parent(Path(sys.argv[2]))
    else:
        run()
