import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from workgate.control.state import ControlState, ExecutorTrustRecord
from workgate.executor.profile import ExecutorProfile, ExecutorProfileStore
from workgate.persistence import FileStateStore
from workgate.protocol.credentials import (
    executor_credential_verifier,
    new_executor_credential,
)
from workgate.protocol.ids import new_executor_id

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"


class ToolClient(Protocol):
    async def list_tools(self) -> set[str]: ...

    async def call_tool(
        self, name: str, args: dict[str, Any] | None = None
    ) -> Any: ...


@dataclass(frozen=True)
class E2EExecutor:
    executor_id: str
    workspace: Path


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def server_env(
    workspace_root: Path,
    *,
    mode: str,
    port: int | None = None,
    agent_bridge_enabled: bool = False,
    state_dir: Path | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    pythonpath = str(SRC_ROOT)
    if env.get("PYTHONPATH"):
        pythonpath = f"{pythonpath}{os.pathsep}{env['PYTHONPATH']}"
    env.update(
        {
            "PYTHONPATH": pythonpath,
            "WORKGATE_WORKSPACE_ROOT": str(workspace_root),
            "WORKGATE_STATE_DIR": str(
                state_dir or workspace_root / ".workgate"
            ),
            "WORKGATE_MODE": mode,
            "WORKGATE_HOST": "127.0.0.1",
            "WORKGATE_AUTH_MODE": "none",
            "WORKGATE_AGENT_BRIDGE_ENABLED": str(agent_bridge_enabled).lower(),
            "WORKGATE_REMOTE_ENABLED": "false",
            "WORKGATE_RUN_SHELL_DEFAULT_TIMEOUT_S": "5",
            "WORKGATE_RUN_SHELL_MAX_TIMEOUT_S": "10",
            "WORKGATE_TOOL_TIMEOUT_S": "15",
        }
    )
    if port is not None:
        env["WORKGATE_PORT"] = str(port)
        env["WORKGATE_BASE_URL"] = f"http://127.0.0.1:{port}"
    return env


def provision_executor_pair(
    *,
    control_state_dir: Path,
    executor_state_dir: Path,
    control_url: str,
    name: str = "e2e-executor",
) -> str:
    """Seed matching production trust/profile state for non-interactive E2E setup."""
    executor_id = new_executor_id()
    credential = new_executor_credential()

    control_store = FileStateStore(lambda: control_state_dir)
    state = ControlState(control_store)
    state.start()
    try:
        state.put_executor(
            ExecutorTrustRecord(
                executor_id=executor_id,
                name=name,
                credential_verifier=executor_credential_verifier(credential),
                created_at=time.time(),
            )
        )
    finally:
        state.close()

    executor_store = FileStateStore(lambda: executor_state_dir)
    ExecutorProfileStore(executor_store).save(
        ExecutorProfile(
            control_url=control_url,
            executor_id=executor_id,
            credential=credential,
        )
    )
    return executor_id


def start_executor_process(
    workspace_root: Path,
    *,
    state_dir: Path,
    mode: str,
    agent_bridge_enabled: bool = False,
) -> subprocess.Popen[str]:
    """Start one final executor process from a pre-provisioned profile."""
    return subprocess.Popen(
        [sys.executable, "-m", "workgate.main", "executor", "run"],
        cwd=PROJECT_ROOT,
        env=server_env(
            workspace_root,
            mode=mode,
            agent_bridge_enabled=agent_bridge_enabled,
            state_dir=state_dir,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


async def wait_for_executor_online(
    base_url: str,
    process: subprocess.Popen[str],
    executor_id: str,
) -> None:
    """Wait until final authenticated hello makes the seeded executor eligible."""
    deadline = asyncio.get_running_loop().time() + 10
    async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
        while True:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                raise AssertionError(
                    f"executor exited early with code {process.returncode}\n"
                    f"stdout:\n{stdout}\nstderr:\n{stderr}"
                )
            try:
                response = await client.get(f"{base_url}/api/ui/executors")
                if response.status_code == 200:
                    rows = response.json().get("data", {}).get("executors", [])
                    if any(
                        row.get("executor_id") == executor_id
                        and row.get("online") is True
                        for row in rows
                    ):
                        return
            except httpx.HTTPError, json.JSONDecodeError:
                pass
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(
                    f"executor {executor_id} did not become online at {base_url}"
                )
            await asyncio.sleep(0.05)


async def wait_for_http_ready(
    base_url: str, process: subprocess.Popen[str]
) -> None:
    deadline = asyncio.get_running_loop().time() + 10
    async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
        while True:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                raise AssertionError(
                    f"server exited early with code {process.returncode}\n"
                    f"stdout:\n{stdout}\nstderr:\n{stderr}"
                )
            try:
                response = await client.get(f"{base_url}/healthz")
                if (
                    response.status_code == 200
                    and response.json().get("ok") is True
                ):
                    return
            except httpx.HTTPError, json.JSONDecodeError:
                pass
            if asyncio.get_running_loop().time() >= deadline:
                process.terminate()
                stdout, stderr = process.communicate(timeout=2)
                raise AssertionError(
                    f"server did not become ready at {base_url}\n"
                    f"stdout:\n{stdout}\nstderr:\n{stderr}"
                )
            await asyncio.sleep(0.05)


@asynccontextmanager
async def run_http_process_with_executors(
    tmp_path: Path,
    *,
    mode: str,
    executor_workspaces: tuple[Path, ...],
    agent_bridge_enabled: bool = False,
) -> AsyncGenerator[tuple[str, tuple[E2EExecutor, ...]]]:
    """Run one control process plus explicitly separated final executor processes."""
    if not executor_workspaces:
        raise ValueError("at least one executor workspace is required")

    control_workspace = tmp_path / f"control-workspace-{mode}"
    control_workspace.mkdir(parents=True, exist_ok=True)
    port = free_tcp_port()
    base_url = f"http://127.0.0.1:{port}"
    control_state_dir = tmp_path / f"control-state-{mode}"
    executor_specs: list[tuple[E2EExecutor, Path]] = []
    for index, workspace in enumerate(executor_workspaces, start=1):
        workspace.mkdir(parents=True, exist_ok=True)
        state_dir = tmp_path / f"executor-state-{mode}-{index}"
        executor_id = provision_executor_pair(
            control_state_dir=control_state_dir,
            executor_state_dir=state_dir,
            control_url=base_url,
            name=f"e2e-executor-{index}",
        )
        executor_specs.append(
            (
                E2EExecutor(executor_id=executor_id, workspace=workspace),
                state_dir,
            )
        )

    control_process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "workgate.main",
            "server",
            "--mode",
            mode,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--auth-mode",
            "none",
            "--workspace-root",
            str(control_workspace),
            "--state-dir",
            str(control_state_dir),
            "--agent-bridge-enabled",
            str(agent_bridge_enabled).lower(),
            "--remote-enabled",
            "false",
        ],
        cwd=PROJECT_ROOT,
        env=server_env(
            control_workspace,
            mode=mode,
            port=port,
            agent_bridge_enabled=agent_bridge_enabled,
            state_dir=control_state_dir,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    executor_processes: list[subprocess.Popen[str]] = []
    try:
        await wait_for_http_ready(base_url, control_process)
        for executor, state_dir in executor_specs:
            child = start_executor_process(
                executor.workspace,
                state_dir=state_dir,
                mode=mode,
                agent_bridge_enabled=agent_bridge_enabled,
            )
            executor_processes.append(child)
            await wait_for_executor_online(
                base_url, child, executor.executor_id
            )
        yield (
            base_url,
            tuple(executor for executor, _state_dir in executor_specs),
        )
    finally:
        for child in (*reversed(executor_processes), control_process):
            if child.poll() is not None:
                continue
            child.terminate()
            try:
                child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=5)


@asynccontextmanager
async def run_http_process(
    tmp_path: Path,
    *,
    mode: str,
    agent_bridge_enabled: bool = False,
    workspace_setup: Callable[[Path], None] | None = None,
) -> AsyncGenerator[tuple[str, Path]]:
    """Run one control process and one final executor for normal process E2E tests."""
    workspace = tmp_path / f"workspace-{mode}"
    workspace.mkdir(parents=True, exist_ok=True)
    if workspace_setup is not None:
        workspace_setup(workspace)
    async with run_http_process_with_executors(
        tmp_path,
        mode=mode,
        executor_workspaces=(workspace,),
        agent_bridge_enabled=agent_bridge_enabled,
    ) as (base_url, executors):
        yield base_url, executors[0].workspace


def decode_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def unwrap_tool_payload(value: Any) -> Any:
    decoded = decode_jsonish(value)
    if (
        isinstance(decoded, dict)
        and decoded.get("ok") is True
        and "data" in decoded
    ):
        return decoded["data"]
    return decoded


REST_ROUTES: dict[str, tuple[str, str]] = {
    "audit_tail": ("GET", "/tools/audit_tail"),
    "agent_config_status": ("GET", "/tools/agent_config_status"),
    "list_agent_skills": ("GET", "/tools/list_agent_skills"),
    "activate_agent_skill": ("POST", "/tools/activate_agent_skill"),
    "read_agent_skill_file": ("POST", "/tools/read_agent_skill_file"),
    "bash": ("POST", "/tools/bash"),
    "job": ("POST", "/tools/job"),
    "session_start": ("POST", "/tools/session_start"),
    "session_change_cwd": ("POST", "/tools/session_change_cwd"),
    "session_copy": ("POST", "/tools/session_copy"),
    "read": ("POST", "/tools/read"),
    "search": ("POST", "/tools/search"),
    "workspace_search": ("POST", "/tools/workspace_search"),
    "fetch": ("POST", "/tools/fetch"),
    "list_files": ("POST", "/tools/list_files"),
    "tree_view": ("POST", "/tools/tree"),
    "glob_search": ("POST", "/tools/glob"),
    "write_file": ("POST", "/tools/write_file"),
    "edit_lines": ("POST", "/tools/edit_lines"),
    "hashline_edit": ("POST", "/tools/hashline_edit"),
    "apply_patch": ("POST", "/tools/apply_patch"),
    "delete_file_or_dir": ("POST", "/tools/delete"),
    "create_file_link": ("POST", "/tools/file_link/create"),
    "list_file_links": ("GET", "/tools/file_link/list"),
    "revoke_file_link": ("POST", "/tools/file_link/revoke"),
    "secret_scan": ("POST", "/tools/secret_scan"),
    "run_python_code": ("POST", "/tools/run_python_code"),
    "list_persistent_shells": ("GET", "/tools/list_persistent_shells"),
    "send_persistent_shell_input": (
        "POST",
        "/tools/send_persistent_shell_input",
    ),
    "resize_persistent_shell": ("POST", "/tools/resize_persistent_shell"),
    "read_persistent_shell_output": (
        "POST",
        "/tools/read_persistent_shell_output",
    ),
    "kill_persistent_shell": ("POST", "/tools/kill_persistent_shell"),
    "read_todos": ("GET", "/tools/todo"),
    "write_todos": ("POST", "/tools/todo"),
}


@dataclass
class RestToolClient:
    base_url: str

    async def list_tools(self) -> set[str]:
        return set(REST_ROUTES)

    async def call_tool(
        self, name: str, args: dict[str, Any] | None = None
    ) -> Any:
        method, path = REST_ROUTES[name]
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=20, trust_env=False
        ) as client:
            if method == "GET":
                response = await client.get(path, params=args or {})
            else:
                response = await client.post(path, json=args or {})
        response.raise_for_status()
        return unwrap_tool_payload(response.json())


class McpSessionToolClient:
    def __init__(self, session: ClientSession):
        self._session = session

    async def list_tools(self) -> set[str]:
        result = await self._session.list_tools()
        return {tool.name for tool in result.tools}

    async def call_tool_result(
        self, name: str, args: dict[str, Any] | None = None
    ) -> Any:
        try:
            async with asyncio.timeout(45):
                return await self._session.call_tool(name, args or {})
        except TimeoutError:
            raise AssertionError(f"MCP tool call timed out: {name}") from None

    async def call_tool(
        self, name: str, args: dict[str, Any] | None = None
    ) -> Any:
        result = await self.call_tool_result(name, args)
        error_text = (
            getattr(result.content[0], "text", "") if result.content else ""
        )
        assert not result.isError, error_text
        assert result.content
        text = getattr(result.content[0], "text", "")
        return unwrap_tool_payload(text)


@asynccontextmanager
async def streamable_http_tool_client(
    base_url: str,
) -> AsyncGenerator[McpSessionToolClient]:
    async with (
        httpx.AsyncClient(timeout=20, trust_env=False) as client,
        streamable_http_client(f"{base_url}/mcp", http_client=client) as (
            read,
            write,
            _,
        ),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield McpSessionToolClient(session)


@asynccontextmanager
async def stdio_tool_client(
    tmp_path: Path,
) -> AsyncGenerator[tuple[McpSessionToolClient, Path]]:
    workspace = tmp_path / "workspace-stdio"
    workspace.mkdir()
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "workgate.main",
            "server",
            "--mode",
            "stdio",
            "--auth-mode",
            "none",
            "--workspace-root",
            str(workspace),
            "--agent-bridge-enabled",
            "false",
            "--remote-enabled",
            "false",
        ],
        cwd=str(PROJECT_ROOT),
        env=server_env(workspace, mode="stdio"),
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield McpSessionToolClient(session), workspace


async def assert_required_tools(client: ToolClient, required: set[str]) -> None:
    tools = await client.list_tools()
    missing = required - tools
    assert not missing, f"missing tools: {sorted(missing)}"


pytestmark = pytest.mark.integration
