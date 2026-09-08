import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e_helpers import (
    PROJECT_ROOT,
    ToolClient,
    free_tcp_port,
    server_env,
    streamable_http_tool_client,
    wait_for_http_ready,
)
from workgate import __version__

pytestmark = pytest.mark.integration

REMOTE_TOOL_NAMES = {
    "remote_admin",
    "session_start",
    "read",
    "search",
    "edit_lines",
    "bash",
    "job",
    "view_image",
    "create_file_link",
    "list_agent_skills",
    "activate_agent_skill",
    "read_agent_skill_file",
}


def start_logged_process(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
) -> subprocess.Popen[Any]:
    """Launch a long-lived child without bounded PIPE buffers."""
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        return subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdout=stdout,
            stderr=stderr,
        )


def process_logs(workspace: Path, prefix: str) -> str:
    stdout_path = workspace / f"{prefix}.stdout.log"
    stderr_path = workspace / f"{prefix}.stderr.log"
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    return f"stdout:\n{stdout}\nstderr:\n{stderr}"


def legacy_worker_access(machine: str) -> str:
    """Return deterministic test-only bearer material for a seeded legacy worker."""
    digest = hashlib.sha256(f"legacy-e2e:{machine}".encode()).hexdigest()
    return f"legacy_e2e_{digest}"


def _seed_legacy_worker_registry(
    control_workspace: Path,
    workers: Mapping[str, Path],
) -> None:
    """Seed private legacy trust without restoring the retired public invite flow."""
    state_dir = control_workspace / ".workgate"
    state_dir.mkdir(parents=True, exist_ok=True)
    registry = state_dir / "remote-workers.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "workers": [
                    {
                        "name": machine,
                        "access": legacy_worker_access(machine),
                        "workdir": str(workdir),
                        "created_at": 1.0,
                        "capabilities": [],
                        "info": {},
                    }
                    for machine, workdir in sorted(workers.items())
                ],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    if os.name != "nt":
        registry.chmod(0o600)


@asynccontextmanager
async def run_remote_enabled_mcp_process(
    tmp_path: Path,
    *,
    legacy_workers: Mapping[str, Path] | None = None,
) -> AsyncGenerator[tuple[str, Path, Path]]:
    control_workspace = tmp_path / "workspace-control"
    remote_workspace = tmp_path / "workspace-remote"
    control_workspace.mkdir()
    remote_workspace.mkdir()
    if legacy_workers:
        _seed_legacy_worker_registry(control_workspace, legacy_workers)
    port = free_tcp_port()
    base_url = f"http://127.0.0.1:{port}"
    env = server_env(control_workspace, mode="mcp", port=port)
    env.update(
        {
            "WORKGATE_REMOTE_ENABLED": "true",
            "WORKGATE_REMOTE_POLL_TIMEOUT_S": "1",
            "WORKGATE_REMOTE_JOB_TIMEOUT_S": "15",
        }
    )
    process = start_logged_process(
        [
            sys.executable,
            "-m",
            "workgate.main",
            "server",
            "--mode",
            "mcp",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--auth-mode",
            "none",
            "--workspace-root",
            str(control_workspace),
            "--agent-bridge-enabled",
            "true",
            "--remote-enabled",
            "true",
            "--remote-poll-timeout-s",
            "1",
            "--remote-job-timeout-s",
            "15",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        stdout_path=control_workspace / "server.stdout.log",
        stderr_path=control_workspace / "server.stderr.log",
    )
    try:
        await wait_for_http_ready(base_url, process)
        yield base_url, control_workspace, remote_workspace
    finally:
        terminate_process(process)


def worker_env(remote_workspace: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(
        {
            "WORKGATE_WORKSPACE_ROOT": str(remote_workspace),
            "WORKGATE_STATE_DIR": str(remote_workspace / ".workgate"),
            "WORKGATE_WORKER_STATE_DIR": str(
                remote_workspace / ".workgate-worker-state"
            ),
            "WORKGATE_WORKER_DATA_DIR": str(
                remote_workspace / ".workgate-worker-data"
            ),
            "WORKGATE_AUTH_MODE": "none",
            "WORKGATE_AGENT_BRIDGE_ENABLED": "true",
            "WORKGATE_RUN_SHELL_DEFAULT_TIMEOUT_S": "5",
            "WORKGATE_RUN_SHELL_MAX_TIMEOUT_S": "10",
            "WORKGATE_TOOL_TIMEOUT_S": "15",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return env


def start_worker_process(
    base_url: str,
    access: str,
    machine: str,
    remote_workspace: Path,
    bundle_path: Path,
) -> subprocess.Popen[Any]:
    data_dir = remote_workspace / ".workgate-worker-data"
    digest = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    runtime_dir = data_dir / "runtimes" / digest
    runtime_dir.mkdir(parents=True)
    with tarfile.open(bundle_path) as bundle:
        bundle.extractall(runtime_dir, filter="data")
    (runtime_dir / "runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bundle_version": __version__,
                "sha256": digest,
                "size": bundle_path.stat().st_size,
                "installed_at": 0,
            }
        ),
        encoding="utf-8",
    )
    uv = shutil.which("uv")
    assert uv is not None
    data_dir.mkdir(parents=True, exist_ok=True)
    persisted_uv = data_dir / ("uv.exe" if os.name == "nt" else "uv")
    shutil.copy2(uv, persisted_uv)
    if os.name != "nt":
        persisted_uv.chmod(0o700)

    sync_env = worker_env(remote_workspace)
    sync_env["UV_PROJECT_ENVIRONMENT"] = str(runtime_dir / ".venv")
    sync_env.pop("VIRTUAL_ENV", None)
    synced = subprocess.run(
        [
            str(persisted_uv),
            "sync",
            "--locked",
            "--only-group",
            "worker",
            "--no-install-project",
            "--no-config",
            "--python",
            sys.executable,
        ],
        cwd=runtime_dir,
        env=sync_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert synced.returncode == 0, synced.stderr

    worker_python = (
        runtime_dir / ".venv" / "Scripts" / "python.exe"
        if os.name == "nt"
        else runtime_dir / ".venv" / "bin" / "python"
    )
    isolation_probe = subprocess.run(
        [
            str(worker_python),
            "-c",
            (
                "import importlib.util; "
                "print(importlib.util.find_spec('workgate')); "
                "print(bool(importlib.util.find_spec('pydantic'))); "
                "print(bool(importlib.util.find_spec('yaml')))"
            ),
        ],
        cwd=remote_workspace,
        env=worker_env(remote_workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    assert isolation_probe.returncode == 0, isolation_probe.stderr
    assert isolation_probe.stdout.splitlines() == ["None", "True", "True"]

    profile_id = "p_e2e00000"
    profile_dir = (
        remote_workspace / ".workgate-worker-state" / "profiles" / profile_id
    )
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_path = profile_dir / "profile.json"
    identity_path = profile_dir / "identity.json"
    profile_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile_id": profile_id,
                "runtime_sha256": digest,
                "runtime_version": __version__,
                "server": base_url,
                "name": machine,
                "workdir": str(remote_workspace),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    identity_path.write_text(
        json.dumps(
            {
                "server": base_url,
                "name": machine,
                "access": access,
                "workdir": str(remote_workspace),
                "profile_id": profile_id,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    if os.name != "nt":
        profile_path.chmod(0o600)
        identity_path.chmod(0o600)

    env = worker_env(remote_workspace)
    env["PYTHONPATH"] = str(runtime_dir)
    env["WORKGATE_WORKER_RUNTIME_SHA256"] = digest
    return start_logged_process(
        [
            str(worker_python),
            "-m",
            "workgate.remote_worker",
            "run",
            profile_id,
        ],
        cwd=runtime_dir,
        env=env,
        stdout_path=remote_workspace / "worker.stdout.log",
        stderr_path=remote_workspace / "worker.stderr.log",
    )


def terminate_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


async def wait_for_machine(
    client: ToolClient,
    process: subprocess.Popen[Any],
    machine: str,
    remote_workspace: Path,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + 10
    while True:
        if process.poll() is not None:
            raise AssertionError(
                f"worker exited early with code {process.returncode}\n"
                f"{process_logs(remote_workspace, 'worker')}"
            )
        inventory_result = await client.call_tool(
            "remote_admin", {"action": "list", "args": {}}
        )
        inventory = inventory_result["data"]
        for row in inventory.get("machines", []):
            if row.get("name") == machine and row.get("status") == "online":
                return row
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"remote worker {machine!r} did not become online; "
                f"last inventory: {inventory}"
            )
        await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_legacy_remote_worker_bundle_and_inventory_remain_available(
    tmp_path: Path,
):
    machine = "e2e-remote"
    seeded_remote_workspace = tmp_path / "workspace-remote"
    async with (
        run_remote_enabled_mcp_process(
            tmp_path,
            legacy_workers={machine: seeded_remote_workspace},
        ) as (
            base_url,
            control_workspace,
            remote_workspace,
        ),
        streamable_http_tool_client(base_url) as client,
    ):
        tools = await client.list_tools()
        assert "remote_admin" in tools
        assert "remote" not in tools

        async with httpx.AsyncClient(
            timeout=20, trust_env=False
        ) as http_client:
            bundle_response = await http_client.get(
                f"{base_url}/remote/worker-bundle.tgz"
            )
        bundle_response.raise_for_status()
        bundle_path = tmp_path / "worker-bundle.tgz"
        bundle_path.write_bytes(bundle_response.content)
        with tarfile.open(bundle_path) as bundle:
            names = bundle.getnames()
            assert "pyproject.toml" in names
            assert "uv.lock" in names
            assert "workgate/remote_worker/__main__.py" in names
            assert "workgate/remote_worker/worker.py" in names
            assert "workgate/remote_worker/compat.py" not in names
            assert "workgate/remote_worker/profiles.py" in names
            assert "workgate/remote_worker/runtime.py" in names
            assert "workgate/remote_worker/cli.py" in names
            assert "workgate/remote_worker/service.py" in names
            assert "workgate/remote/join_worker.sh" not in names
            assert "workgate/remote/manager.py" not in names
            assert "workgate/remote/http.py" not in names
            assert not any(name.startswith("vendor/") for name in names)
            assert not any(
                name.startswith("workgate/ui/static/") or "ui_static" in name
                for name in names
            )
            assert all(
                name in {"pyproject.toml", "uv.lock"} or name.endswith(".py")
                for name in names
            )

        worker = start_worker_process(
            base_url,
            legacy_worker_access(machine),
            machine,
            remote_workspace,
            bundle_path,
        )
        try:
            row = await wait_for_machine(
                client, worker, machine, remote_workspace
            )
            assert row["workdir"] == str(remote_workspace)

            marker = remote_workspace / "legacy-marker.txt"
            marker.write_text(
                "legacy worker compatibility only", encoding="utf-8"
            )
            assert not (control_workspace / marker.name).exists()

            revoked = await client.call_tool(
                "remote_admin",
                {"action": "revoke", "args": {"machine": machine}},
            )
            assert revoked == {
                "action": "revoke",
                "data": {"machine": machine, "revoked": True},
            }
        finally:
            terminate_process(worker)
