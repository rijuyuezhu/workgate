"""Native Windows CLI device pairing: actual owner approval and denial."""

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
    raise RuntimeError("This smoke needs native Windows Python")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tests.e2e.helpers import (  # noqa: E402
    free_tcp_port,
    isolated_process_env,
    isolated_xdg_env,
    start_control_process,
    stop_process,
    write_yaml_config,
)
from workgate.executor.profile import ExecutorProfileStore  # noqa: E402
from workgate.persistence import FileStateStore  # noqa: E402


def _pair_one(root: Path, url: str, *, approve: bool) -> None:
    state = root / ("approved" if approve else "denied")
    state.mkdir()
    config = write_yaml_config(
        root / f"{'approve' if approve else 'deny'}.yaml",
        {"state_dir": str(state)},
    )
    output = root / f"{'approve' if approve else 'deny'}.out"
    errors = root / f"{'approve' if approve else 'deny'}.err"
    with (
        output.open("w", encoding="utf-8") as stdout,
        errors.open("w", encoding="utf-8") as stderr,
    ):
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "workgate.main",
                "executor",
                "connect",
                url,
                "--name",
                "native-windows-test",
                "--config",
                str(config),
            ],
            env=isolated_process_env(),
            stdout=stdout,
            stderr=stderr,
        )
    try:
        deadline = time.monotonic() + 35
        code = None
        with httpx.Client(timeout=3, trust_env=False) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(
                        f"executor connect exited before owner approval: {errors.read_text(errors='replace')}"
                    )
                match = re.search(
                    r"^Code: (\S+)$",
                    output.read_text(encoding="utf-8"),
                    flags=re.MULTILINE,
                )
                if match is not None:
                    code = match.group(1)
                    break
                time.sleep(0.1)
            assert code, (
                "executor connect never displayed the pairing user code"
            )
            detail = client.get(f"{url}/api/ui/pair", params={"code": code})
            assert detail.status_code == 200, detail.text
            assert (
                detail.json()["data"]["requested_name"] == "native-windows-test"
            )
            decision = client.post(
                f"{url}/api/ui/pair",
                json={
                    "user_code": code,
                    "decision": "approve" if approve else "deny",
                    "name": "native-approved" if approve else None,
                },
            )
            assert decision.status_code == 200, decision.text
        assert process.wait(timeout=35) == (0 if approve else 1), (
            errors.read_text(encoding="utf-8", errors="replace")
        )
        profile = ExecutorProfileStore(FileStateStore(lambda: state)).load()
        if approve:
            assert profile is not None and profile.control_url == url
            assert (
                f"Paired executor: {profile.executor_id}"
                in output.read_text(encoding="utf-8")
            )
        else:
            assert profile is None, (
                "denied pairing wrote an executor credential"
            )
        print(
            "Native Windows device-code owner approval: PASS"
            if approve
            else "Native Windows pairing denial leaves no profile: PASS"
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def main() -> None:
    root = Path(tempfile.gettempdir()) / f"workgate-pair-390-{uuid4().hex}"
    root.mkdir()
    control = None
    try:
        port = free_tcp_port()
        url = f"http://127.0.0.1:{port}"
        control_config = write_yaml_config(
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
        control = start_control_process(
            control_config,
            env=isolated_xdg_env(root / "xdg"),
            stdout_path=root / "control.out",
            stderr_path=root / "control.err",
        )
        with httpx.Client(timeout=2, trust_env=False) as client:
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                if control.poll() is not None:
                    raise AssertionError(
                        f"Control failed: {(root / 'control.err').read_text(errors='replace')}"
                    )
                try:
                    if client.get(f"{url}/healthz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("Control never became ready")
        _pair_one(root, url, approve=True)
        _pair_one(root, url, approve=False)
    finally:
        if control is not None:
            stop_process(control)
        for attempt in range(40):
            try:
                shutil.rmtree(root)
                break
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.15)


if __name__ == "__main__":
    main()
