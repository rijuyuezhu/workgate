"""Windows CI: real frozen ZIP + PowerShell bootstrap + owner-approved service."""

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
from workgate.executor.service import (  # noqa: E402
    _WINDOWS_TASK_DESCRIPTION,
    _WINDOWS_TASK_NAME,
)
from workgate.platform.windows_task import WindowsUserTask  # noqa: E402


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, check=True, capture_output=True, text=True, timeout=25
    )


def main() -> None:
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
        source = root / "control.py"
        source.write_text(
            "from pathlib import Path\n"
            "from workgate.control import executor_bootstrap as b\n"
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
            shell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
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
                match = re.search(
                    r"^Code: (\S+)$",
                    (root / "bootstrap.out").read_text(errors="replace"),
                    flags=re.MULTILINE,
                )
                if match:
                    code = match.group(1)
                    break
                time.sleep(0.2)
            assert code, "frozen bootstrap never reached real pairing"
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
            asyncio.run(
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
            subprocess.run(
                [str(install), "executor", "uninstall-service"],
                env=env,
                check=False,
                capture_output=True,
                timeout=70,
            )
        # Only the disposable CI runner is eligible for this smoke; fail closed
        # if the service is still registered rather than printing PASS.
        if task.status() is not None:
            task.uninstall()
        assert task.status() is None, "frozen Executor task cleanup failed"
        if control is not None:
            stop_process(control)
        for attempt in range(40):
            try:
                shutil.rmtree(root)
                break
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.25)
    print(
        "Native frozen Windows bootstrap ZIP, paired persistent service and checksum rollback: PASS"
    )


if __name__ == "__main__":
    main()
