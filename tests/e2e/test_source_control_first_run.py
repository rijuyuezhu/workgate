"""Real first-run Control CLI and HTTP health under disposable user state."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.e2e.helpers import (
    free_tcp_port,
    isolated_process_env,
    isolated_xdg_env,
    start_control_process,
    stop_process,
    wait_for_http_ready,
)


@pytest.mark.asyncio
async def test_first_run_control_initialization_and_service_process(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o700)
    config = config_dir / "control.yaml"

    env = isolated_xdg_env(tmp_path / "xdg")
    env["HOME"] = str(tmp_path)
    # Native Windows uses AppData rather than XDG.
    for key in ("APPDATA", "LOCALAPPDATA"):
        directory = tmp_path / key.lower()
        directory.mkdir()
        env[key] = str(directory)
    child_env = isolated_process_env(env)
    port = free_tcp_port()

    init = [
        sys.executable,
        "-m",
        "workgate.main",
        "control",
        "init-config",
        "--config",
        str(config),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    initial = subprocess.run(
        init,
        env=child_env,
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert "created" in initial.stdout
    if os.name != "nt":
        assert config.stat().st_mode & 0o077 == 0

    values = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert values["mode"] == "http"
    assert values["auth_mode"] == "oauth"
    assert values["oauth_admin_pin"]
    assert values["oauth_admin_pin"] not in initial.stdout + initial.stderr

    before = hashlib.sha256(config.read_bytes()).digest()
    repeated = subprocess.run(
        init,
        env=child_env,
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert "preserved" in repeated.stdout
    assert hashlib.sha256(config.read_bytes()).digest() == before
    assert values["oauth_admin_pin"] not in repeated.stdout + repeated.stderr

    stdout = tmp_path / "control.stdout"
    stderr = tmp_path / "control.stderr"
    process = start_control_process(
        config, env=env, stdout_path=stdout, stderr_path=stderr
    )
    try:
        await wait_for_http_ready(
            f"http://127.0.0.1:{port}",
            process,
            stdout_path=stdout,
            stderr_path=stderr,
            timeout_s=25,
        )
    finally:
        stop_process(process)
