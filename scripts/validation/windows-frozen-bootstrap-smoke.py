"""Windows CI: real frozen ZIP + PowerShell bootstrap + owner-approved service."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

if os.name != "nt":
    raise RuntimeError("This smoke requires native Windows Python")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tests.e2e.helpers import (  # noqa: E402
    free_tcp_port,
    isolated_process_env,
    stop_process,
    wait_for_executor_online,
    write_yaml_config,
)
from workgate import __version__  # noqa: E402
from workgate.config.executor import resolve_executor_config  # noqa: E402
from workgate.config.settings import Settings  # noqa: E402
from workgate.executor.runtime_update import (  # noqa: E402
    ExecutorRuntimeState,
    ExecutorRuntimeStateStore,
    ExecutorRuntimeUpdateStatus,
    _validate_windows_service_executable,
    apply_control_managed_update,
)
from workgate.executor.service import (  # noqa: E402
    _WINDOWS_TASK_DESCRIPTION,
    _WINDOWS_TASK_NAME,
    ExecutorServiceManager,  # noqa: E402
)
from workgate.platform.windows_task import WindowsUserTask  # noqa: E402
from workgate.protocol.executor import ExecutorRuntimeOwnership  # noqa: E402


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, check=True, capture_output=True, text=True, timeout=25
    )


def _remove_tree(path: Path) -> None:
    for attempt in range(60):
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            if attempt == 59:
                raise
            time.sleep(0.5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--older-service", type=Path)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Frozen bootstrap service smoke is CI-only")
    archive = Path("release/workgate-windows-x86_64.zip").resolve()
    checksum = Path("release/workgate-windows-x86_64.sha256").resolve()
    assert archive.is_file() and checksum.is_file(), (
        "Build/package the real Windows EXEs first"
    )
    with archive.open("rb") as content:
        digest = hashlib.file_digest(content, "sha256").hexdigest()
    assert digest == checksum.read_text(encoding="ascii").split()[0]

    # Never overwrite a real user's task: only run this on a disposable CI runner.
    task = WindowsUserTask(
        _WINDOWS_TASK_NAME, description=_WINDOWS_TASK_DESCRIPTION, run=_run
    )
    assert task.status() is None, (
        "refusing to replace an installed Workgate service"
    )

    root = (
        Path(tempfile.gettempdir()) / f"workgate-frozen-bootstrap-{uuid4().hex}"
    )
    root.mkdir()
    control = None
    install = None
    boot = None
    uninstall_error = None
    env = isolated_process_env()
    env["WORKGATE_STATE_DIR"] = str(root / "executor-state")
    env["WORKGATE_DEFAULT_WORKDIR"] = str(root / "workspace with spaces")
    Path(env["WORKGATE_DEFAULT_WORKDIR"]).mkdir()
    try:
        port = free_tcp_port()
        url = f"http://127.0.0.1:{port}"
        config = write_yaml_config(
            root / "control.yaml",
            {
                "mode": "http",
                "host": "127.0.0.1",
                "port": port,
                "base_url": url,
                "auth_mode": "none",
                "state_dir": str(root / "control-state"),
                "data_dir": str(root / "control-data"),
                "agent_bridge_enabled": False,
            },
        )
        # Use the real Control bootstrap routes, but serve the *locally built*
        # ZIP instead of requiring a published release for an unreleased SHA.
        # PowerShell may buffer the nested frozen CLI's stdout until pairing
        # completes. Observe the real Control pairing event in this test-only
        # process, then approve through the unmodified owner HTTP endpoint.
        source = root / "control.py"
        source.write_text(
            "from pathlib import Path\n"
            "from workgate.control import executor_bootstrap as b\n"
            "from workgate.control import pairing as p\n"
            f"_code_file = Path({str(root / 'pair-code.txt')!r})\n"
            "_start_pairing = p.ExecutorPairingService.start_pairing\n"
            "async def _traced_start_pairing(self, request):\n"
            "    result = await _start_pairing(self, request)\n"
            "    _code_file.write_text(result.user_code, encoding='ascii')\n"
            "    return result\n"
            "p.ExecutorPairingService.start_pairing = _traced_start_pairing\n"
            f"_archive = Path({str(archive)!r})\n"
            f"_checksum = Path({str(root / 'digest.txt')!r})\n"
            "async def _fetch_checksum(target):\n"
            "    assert target == 'windows-x86_64'\n"
            "    return _checksum.read_text(encoding='ascii').strip()\n"
            "async def _archive_stream(target):\n"
            "    assert target == 'windows-x86_64'\n"
            "    with _archive.open('rb') as file:\n"
            "        while chunk := file.read(65536):\n"
            "            yield chunk\n"
            "b._fetch_checksum = _fetch_checksum\n"
            "b._archive_stream = _archive_stream\n"
            "from workgate.main import main\n"
            f"main(['control', '--config', {str(config)!r}])\n",
            encoding="utf-8",
        )
        (root / "digest.txt").write_text(digest, encoding="ascii")
        with (
            (root / "control.out").open("w") as stdout,
            (root / "control.err").open("w") as stderr,
        ):
            control = subprocess.Popen(
                [sys.executable, str(source)],
                env=isolated_process_env(),
                stdout=stdout,
                stderr=stderr,
            )
        with httpx.Client(timeout=3, trust_env=False) as client:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                assert control.poll() is None, (root / "control.err").read_text(
                    errors="replace"
                )
                try:
                    response = client.get(f"{url}/executor/v1/bootstrap.ps1")
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError(
                    "Control bootstrap did not become available"
                )
            assert "$MaxArchiveBytes" in response.text
            script = root / "bootstrap.ps1"
            script.write_text(response.text, encoding="utf-8")
            # CI itself runs in PowerShell 7; mixing its PSModulePath with
            # Windows PowerShell 5.1 hides built-in cmdlets like Get-FileHash.
            shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
            assert shell
            cmd = [
                shell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-Name",
                "frozen-bootstrap-e2e",
                "-DefaultWorkdir",
                env["WORKGATE_DEFAULT_WORKDIR"],
                "-Persist",
            ]
            with (
                (root / "bootstrap.out").open("w", encoding="utf-8") as stdout,
                (root / "bootstrap.err").open("w", encoding="utf-8") as stderr,
            ):
                boot = subprocess.Popen(
                    cmd, env=env, stdout=stdout, stderr=stderr
                )
            deadline = time.monotonic() + 130
            code = None
            while time.monotonic() < deadline:
                assert boot.poll() is None, (root / "bootstrap.err").read_text(
                    errors="replace"
                )
                challenge = root / "pair-code.txt"
                if challenge.is_file():
                    code = challenge.read_text(encoding="ascii").strip()
                    break
                time.sleep(0.2)
            if code is None:
                output = (root / "bootstrap.out").read_text(errors="replace")
                output = re.sub(
                    r"(?m)^Code: [^\r\n]+", "Code: [redacted]", output
                )
                errors = (root / "bootstrap.err").read_text(errors="replace")
                staged = []
                for item in Path(tempfile.gettempdir()).glob(
                    "workgate-bootstrap-*"
                ):
                    try:
                        staged.append(
                            (
                                item.name,
                                [
                                    (file.name, file.stat().st_size)
                                    for file in item.iterdir()
                                ],
                            )
                        )
                    except OSError:
                        continue
                raise AssertionError(
                    "frozen PowerShell bootstrap never reached pairing; "
                    f"stdout={output[-1800:]!r}, stderr={errors[-1800:]!r}, "
                    f"staged={staged[-3:]!r}"
                )
            inspected = client.get(f"{url}/api/ui/pair", params={"code": code})
            assert inspected.status_code == 200, inspected.text
            assert (
                inspected.json()["data"]["requested_name"]
                == "frozen-bootstrap-e2e"
            )
            approved = client.post(
                f"{url}/api/ui/pair",
                json={
                    "user_code": code,
                    "decision": "approve",
                    "name": "frozen-bootstrap-e2e",
                },
            )
            assert approved.status_code == 200, approved.text
            executor_id = approved.json()["data"]["executor_id"]
            assert boot.wait(timeout=150) == 0, (
                root / "bootstrap.err"
            ).read_text(errors="replace")
            assert "Workgate executor installed and started." in (
                root / "bootstrap.out"
            ).read_text(errors="replace")
            meta = (
                root
                / "executor-state"
                / "executor"
                / "service"
                / "installation.json"
            )
            installation = json.loads(meta.read_text(encoding="utf-8"))
            service_exe = Path(installation["command"][0])
            assert (
                service_exe.name.lower() == "workgatew.exe"
                and service_exe.is_file()
            )
            assert service_exe.with_name("workgate.exe").is_file()
            last_seen = asyncio.run(
                wait_for_executor_online(
                    url,
                    control,
                    executor_id,
                    timeout_s=60,
                    stdout_path=root / "control.out",
                    stderr_path=root / "control.err",
                )
            )

            assert task.status()["state"].lower() == "running"
            install = service_exe.with_name("workgate.exe")
            (root / "digest.txt").write_text("0" * 64, encoding="ascii")
            failed = subprocess.run(
                cmd, env=env, text=True, capture_output=True, timeout=90
            )
            assert (
                failed.returncode != 0
                and "checksum mismatch"
                in (failed.stderr + failed.stdout).lower()
            )
            assert task.status()["state"].lower() == "running", (
                "bad checksum damaged installed service"
            )
            asyncio.run(
                wait_for_executor_online(
                    url,
                    control,
                    executor_id,
                    after_last_seen=last_seen,
                    timeout_s=25,
                    stdout_path=root / "control.out",
                    stderr_path=root / "control.err",
                )
            )
            # Also exercise an explicit switch after the optional automatic
            # A->B upgrade; this uses the current version as its own target.
            task.stop()
            assert task.status()["state"].lower() != "running"
            manager = ExecutorServiceManager(
                resolve_executor_config(
                    Settings(
                        state_dir=Path(env["WORKGATE_STATE_DIR"]),
                        default_workdir=Path(env["WORKGATE_DEFAULT_WORKDIR"]),
                    )
                ),
                executable=install,
            )
            runtime_store = ExecutorRuntimeStateStore(manager.state_store)
            runtime_store.save(
                ExecutorRuntimeState(
                    ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED,
                    executable=str(service_exe),
                )
            )
            if args.older_service is not None:
                # A is a genuinely separate frozen build with a distinct
                # synthetic version; it is NOT a published historical release.
                old_build = args.older_service.resolve(strict=True)
                with old_build.open("rb") as image:
                    old_sha256 = hashlib.file_digest(
                        image, "sha256"
                    ).hexdigest()
                with service_exe.open("rb") as image:
                    assert (
                        hashlib.file_digest(image, "sha256").hexdigest()
                        != old_sha256
                    )
                old_service = (
                    service_exe.parent / "older-build" / "workgatew.exe"
                )
                old_service.parent.mkdir()
                shutil.copyfile(old_build, old_service)
                _validate_windows_service_executable(
                    old_service, "5.0.0a3.dev0"
                )
                manager.activate_windows_runtime(service_exe, old_service)
                runtime_store.save(
                    ExecutorRuntimeState(
                        ownership=ExecutorRuntimeOwnership.CONTROL_MANAGED,
                        executable=str(old_service),
                    )
                )
                action = task._powershell(
                    f"(Get-ScheduledTask {task._task} -ErrorAction Stop).Actions[0].Execute"
                )
                assert (
                    Path(action.stdout.strip()).resolve()
                    == old_service.resolve()
                )
                # Earlier in this smoke we deliberately corrupt the checksum
                # to prove bootstrap rejection. The automatic A->B upgrade
                # must instead see the genuine checksum for the B archive.
                (root / "digest.txt").write_text(digest, encoding="ascii")
                task.start()
                # Real older frozen service receives Control's newer-version
                # policy, fetches B, switches the actual scheduled action and
                # relies on its bounded automatic restart trigger (no test-side
                # call to apply_control_managed_update or task.start for B).
                deadline = time.monotonic() + 170
                while time.monotonic() < deadline:
                    state = runtime_store.load()
                    if (
                        state is not None
                        and state.executable is not None
                        and state.executable
                        not in {str(service_exe), str(old_service)}
                        and state.update_status
                        is ExecutorRuntimeUpdateStatus.IDLE
                    ):
                        candidate = Path(state.executable)
                        if candidate.is_file():
                            _validate_windows_service_executable(
                                candidate, __version__
                            )
                            service_exe = candidate
                            break
                    time.sleep(0.5)
                else:
                    # State and native task status are enough for CI failures;
                    # service.log might contain private executor information.
                    raise AssertionError(
                        "old frozen Executor did not auto-publish newer PE; "
                        f"update_state={runtime_store.load()!r}; "
                        f"task_status={task.status()!r}"
                    )
                action = task._powershell(
                    f"(Get-ScheduledTask {task._task} -ErrorAction Stop).Actions[0].Execute"
                )
                assert (
                    Path(action.stdout.strip()).resolve()
                    == service_exe.resolve()
                )
                # The older A's authenticated hello updates last_seen before
                # Scheduler's delayed B restart. Require the *new* B runtime
                # inventory and a truly running native task before success.
                deadline = time.monotonic() + 120
                observed = None
                while time.monotonic() < deadline:
                    assert control.poll() is None, (
                        "Control exited during upgrade"
                    )
                    response = client.get(f"{url}/api/ui/executors")
                    assert response.status_code == 200, response.text
                    rows = response.json()["data"]["executors"]
                    observed = next(
                        (
                            row
                            for row in rows
                            if row["executor_id"] == executor_id
                        ),
                        None,
                    )
                    if (
                        observed is not None
                        and observed["online"]
                        and (observed.get("runtime") or {}).get(
                            "workgate_version"
                        )
                        == __version__
                        and isinstance(
                            observed.get("last_seen_at"), int | float
                        )
                        and observed["last_seen_at"] > last_seen
                        and task.status()["state"].lower() == "running"
                    ):
                        break
                    time.sleep(0.5)
                else:
                    raise AssertionError(
                        "new frozen Executor never restarted with B runtime: "
                        f"fleet={observed!r}, task={task.status()!r}"
                    )
                last_seen = asyncio.run(
                    wait_for_executor_online(
                        url,
                        control,
                        executor_id,
                        after_last_seen=float(observed["last_seen_at"]),
                        timeout_s=45,
                        stdout_path=root / "control.out",
                        stderr_path=root / "control.err",
                    )
                )
                task.stop()
            (root / "digest.txt").write_text(digest, encoding="ascii")
            published = asyncio.run(
                apply_control_managed_update(
                    control_url=url,
                    target_version=__version__,
                    store=runtime_store,
                    windows_activate=manager.activate_windows_runtime,
                )
            )
            assert published != service_exe and published.is_file()
            state = runtime_store.load()
            assert state is not None and state.executable == str(published)
            assert state.update_status is ExecutorRuntimeUpdateStatus.IDLE
            next_metadata = manager._read_metadata()
            assert next_metadata is not None
            assert next_metadata["command"][0] == str(published)
            action = task._powershell(
                f"(Get-ScheduledTask {task._task} -ErrorAction Stop).Actions[0].Execute"
            )
            assert (
                Path(action.stdout.strip()).resolve() == published.resolve()
            ), "native scheduled task still targets the old frozen executable"
            task.start()
            upgraded_seen = asyncio.run(
                wait_for_executor_online(
                    url,
                    control,
                    executor_id,
                    after_last_seen=last_seen,
                    timeout_s=65,
                    stdout_path=root / "control.out",
                    stderr_path=root / "control.err",
                )
            )
            assert task.status()["state"].lower() == "running"
            (root / "digest.txt").write_text("0" * 64, encoding="ascii")
            with pytest.raises(RuntimeError, match="checksum mismatch"):
                asyncio.run(
                    apply_control_managed_update(
                        control_url=url,
                        target_version=__version__,
                        store=runtime_store,
                        windows_activate=manager.activate_windows_runtime,
                    )
                )
            rejected = runtime_store.load()
            assert rejected is not None
            assert rejected.executable == str(published)
            assert rejected.update_status is ExecutorRuntimeUpdateStatus.FAILED
            (root / "digest.txt").write_text(digest, encoding="ascii")
            with pytest.raises(RuntimeError, match="validation failed"):
                asyncio.run(
                    apply_control_managed_update(
                        control_url=url,
                        target_version="9.9.9",
                        store=runtime_store,
                        windows_activate=manager.activate_windows_runtime,
                    )
                )
            rejected = runtime_store.load()
            assert rejected is not None and rejected.executable == str(
                published
            )
            assert rejected.update_status is ExecutorRuntimeUpdateStatus.FAILED
            assert task.status()["state"].lower() == "running"
            asyncio.run(
                wait_for_executor_online(
                    url,
                    control,
                    executor_id,
                    after_last_seen=upgraded_seen,
                    timeout_s=25,
                    stdout_path=root / "control.out",
                    stderr_path=root / "control.err",
                )
            )
    finally:
        if boot is not None and boot.poll() is None:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(boot.pid)],
                capture_output=True,
                check=False,
                timeout=15,
            )
            boot.wait(timeout=20)
        if install is not None:
            removed = subprocess.run(
                [str(install), "executor", "uninstall-service"],
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=70,
            )
            if removed.returncode:
                uninstall_error = (removed.stderr or removed.stdout)[-1500:]
        # Only the disposable CI runner is eligible for this smoke; fail closed
        # if the service is still registered rather than printing PASS.
        if task.status() is not None:
            task.uninstall()
        assert task.status() is None, "frozen Executor task cleanup failed"
        if control is not None:
            stop_process(control)
        _remove_tree(root)
        if install is not None:
            # Remove only updater-created detached runtimes after full service stop.
            for runtime in install.parent.glob(".workgate.runtime.*.exe"):
                runtime.unlink()
            # Bootstrap creates a unique versioned directory under LOCALAPPDATA.
            runtime_root = (
                Path(env["LOCALAPPDATA"]) / "workgate" / "executor-bootstrap"
            )
            if install.parent.parent != runtime_root:
                raise AssertionError(
                    "refusing to clean an unexpected install directory"
                )
            _remove_tree(install.parent)
        if uninstall_error is not None:
            raise AssertionError(
                f"frozen Executor uninstall failed: {uninstall_error}"
            )
    print(
        "Native frozen Windows bootstrap, managed upgrade, rejected invalid "
        "updates and safe uninstall: PASS"
    )


if __name__ == "__main__":
    main()
