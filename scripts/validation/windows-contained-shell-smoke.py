"""Exercise real Windows bounded commands and Job Object descendant containment."""

import asyncio
import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("Run with native Windows CPython")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workgate.config.executor import resolve_executor_config  # noqa: E402
from workgate.config.settings import clear_settings_cache, get_settings  # noqa: E402
from workgate.executor.bounded_runner import bounded_runner_argv  # noqa: E402
from workgate.executor.shell import run_shell  # noqa: E402
from workgate.platform.shell import shell_join_argv  # noqa: E402
from workgate.platform.windows_job import WindowsJob  # noqa: E402


def wait_dead(pid: int) -> bool:
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


async def wait_for_pidfile(path: Path) -> int:
    for _ in range(100):
        if path.is_file():
            return int(path.read_text(encoding="ascii"))
        await asyncio.sleep(0.1)
    raise AssertionError(f"Bounded command did not start a descendant: {path}")


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="workgate-containment-") as temp:
        root = Path(temp)
        os.environ["WORKGATE_DEFAULT_WORKDIR"] = str(root)
        os.environ["WORKGATE_STATE_DIR"] = str(root / ".workgate")
        os.environ["WORKGATE_AGENT_BRIDGE_ENABLED"] = "false"
        clear_settings_cache()
        config = resolve_executor_config(get_settings())
        result = await run_shell(
            config,
            "Write-Output 'Job Bound — 你好'",
            cwd=str(root),
            timeout_s=10,
        )
        assert result.ok and "Job Bound — 你好" in result.stdout, result

        script = root / "create child.py"
        script.write_text(
            "import subprocess,sys,time,pathlib\n"
            "child = subprocess.Popen([sys.executable,'-c',"
            "'import time; time.sleep(60)'],stdin=subprocess.DEVNULL,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii')\n"
            "if len(sys.argv)>2: time.sleep(60)\n",
            encoding="utf-8",
        )

        def command(pidfile: Path, *, wait: bool) -> str:
            return shell_join_argv(
                [
                    sys.executable,
                    str(script),
                    str(pidfile),
                    *(["wait"] if wait else []),
                ],
                shell=config.shell_executable,
            )

        # Running the internal runner without an attachment release must
        # fail before executing any shell code.
        no_gate_marker = root / "no-gate-must-not-run.txt"
        gated = subprocess.run(
            bounded_runner_argv(
                config.shell_executable,
                f"Set-Content -LiteralPath '{no_gate_marker}' -Value 'unsafe'",
            ),
            input=b"",
            capture_output=True,
            cwd=root,
            timeout=15,
        )
        assert gated.returncode == 125, gated
        assert not no_gate_marker.exists()

        # Even a root shell that exits successfully must not leak children.
        natural_pidfile = root / "normal.pid"
        result = await run_shell(
            config,
            command(natural_pidfile, wait=False),
            cwd=str(root),
            timeout_s=15,
        )
        assert result.ok, result
        pid = int(natural_pidfile.read_text(encoding="ascii"))
        assert wait_dead(pid), f"normal completion leaked descendant {pid}"

        timed_pidfile = root / "timed.pid"
        result = await run_shell(
            config,
            command(timed_pidfile, wait=True),
            cwd=str(root),
            timeout_s=3,
        )
        assert result.timed_out, result
        pid = int(timed_pidfile.read_text(encoding="ascii"))
        assert wait_dead(pid), f"timed out command leaked descendant {pid}"

        cancelled_pidfile = root / "cancel.pid"
        task = asyncio.create_task(
            run_shell(
                config,
                command(cancelled_pidfile, wait=True),
                cwd=str(root),
                timeout_s=20,
            )
        )
        pid = await wait_for_pidfile(cancelled_pidfile)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Cancelled bounded command returned success")
        assert wait_dead(pid), f"cancelled command leaked descendant {pid}"

        # A cancellation *during* the async spawn must also reap a child
        # created after the caller has already been cancelled.
        import workgate.platform.windows_job as native_job

        original_launch = native_job.asyncio.create_subprocess_exec
        started = asyncio.Event()
        release = asyncio.Event()
        late_child = None
        late_marker = root / "late-start-must-not-run.txt"

        async def delayed_launch(*args, **kwargs):
            nonlocal late_child
            late_child = await original_launch(*args, **kwargs)
            started.set()
            await release.wait()
            return late_child

        native_job.asyncio.create_subprocess_exec = delayed_launch
        try:
            launch_task = asyncio.create_task(
                native_job.spawn_bounded_runner(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--wait-gate",
                        str(late_marker),
                    ],
                    cwd=str(root),
                    env=os.environ.copy(),
                )
            )
            await asyncio.wait_for(started.wait(), timeout=10)
            launch_task.cancel()
            try:
                await launch_task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("Cancelled startup unexpectedly succeeded")
            release.set()
            assert late_child is not None
            await asyncio.wait_for(late_child.wait(), timeout=10)
            assert wait_dead(late_child.pid)
            assert not late_marker.exists()
        finally:
            release.set()
            native_job.asyncio.create_subprocess_exec = original_launch

        # A failed job attachment must never release a command to execute.
        failed_marker = root / "should-not-run.txt"
        original = WindowsJob.attach_waiting_process

        def refuse(_self: WindowsJob, _pid: int) -> None:
            raise OSError("simulated Windows job assignment failure")

        WindowsJob.attach_waiting_process = refuse
        try:
            try:
                await run_shell(
                    config,
                    f"Set-Content -LiteralPath '{failed_marker}' -Value 'unsafe'",
                    cwd=str(root),
                    timeout_s=10,
                )
            except OSError as exc:
                assert "job assignment failure" in str(exc)
            else:
                raise AssertionError("Uncontained command was accepted")
            assert not failed_marker.exists()
        finally:
            WindowsJob.attach_waiting_process = original

    print(
        "Native Windows bounded shell: normal, timeout, cancellation and rejected attach PASS"
    )


if __name__ == "__main__":
    if "--wait-gate" in sys.argv:
        if sys.stdin.buffer.read(1) == b"\x01":
            Path(sys.argv[2]).write_text("wrongly released", encoding="utf-8")
    else:
        asyncio.run(main())
