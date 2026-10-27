"""Fail-closed source-cutover checks for process-owned ephemeral resources."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor.runtime import build_executor_runtime
from workgate.protocol.executor import SessionInventorySummary

SESSION_ID = "sess_0000000000000000000001"


@pytest.fixture
def idle_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(default_workdir=workspace, state_dir=tmp_path / "state")
        )
    )
    monkeypatch.setattr(
        runtime, "connection", SimpleNamespace(active_command_count=0)
    )
    monkeypatch.setattr(runtime, "_started", True)
    inventory = SimpleNamespace(
        sessions=(
            SessionInventorySummary(
                session_id=SESSION_ID, workdir=str(workspace)
            ),
        ),
        shells=(),
        jobs=(),
    )

    async def reconnect_inventory():
        return inventory

    async def all_shells(*_args):
        return set()

    monkeypatch.setattr(runtime, "_build_reconnect_hello", reconnect_inventory)
    monkeypatch.setattr(
        "workgate.executor.shell.authoritative_persistent_shell_ids_execute",
        all_shells,
    )
    return runtime


@pytest.mark.asyncio
async def test_cutover_preflight_accepts_empty_ephemeral_services(
    idle_runtime,
) -> None:
    await idle_runtime.require_source_cutover_idle()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource",
    [
        "browser-session",
        "browser-launching",
        "browser-cleanup",
        "browser-closing",
        "browser-profile",
        "peer-receiver",
        "peer-probe",
        "terminal-bridge",
        "pending-terminal-bridge",
        "terminal-operation",
        "conpty",
    ],
)
async def test_ephemeral_resource_blocks_source_cutover(
    idle_runtime, monkeypatch: pytest.MonkeyPatch, resource: str
) -> None:
    runtime = idle_runtime
    if resource == "browser-session":
        runtime.browser._sessions["browser-1"] = object()
    elif resource == "browser-launching":
        runtime.browser._starting = 1
    elif resource == "browser-cleanup":
        runtime.browser._cleanup_pending["browser-1"] = object()
    elif resource == "browser-closing":
        runtime.browser._closing["browser-1"] = object()
    elif resource == "browser-profile":
        runtime.browser._profiles_in_use.add("profile-1")
    elif resource == "peer-receiver":
        runtime.peer_receiver._leases["transfer-1"] = object()
    elif resource == "peer-probe":
        runtime.peer_probe._probes["probe-1"] = object()
    elif resource == "terminal-bridge":
        runtime.terminal_runtime.bridges.bridges["bridge-1"] = object()
    elif resource == "pending-terminal-bridge":
        runtime.terminal_runtime.bridges.pending_bridges.add("bridge-1")
    elif resource == "terminal-operation":
        task = asyncio.current_task()
        assert task is not None
        runtime.terminal_runtime.bridges._operations[task] = 1
    elif resource == "conpty":
        monkeypatch.setattr(
            runtime.terminal_runtime.conpty,
            "active",
            lambda: {"shell-1": object()},
        )
    with pytest.raises(RuntimeError, match="browser, transfer or terminal"):
        await runtime.require_source_cutover_idle()


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["browser", "receiver", "probe"])
async def test_ephemeral_inventory_failure_does_not_grant_cutover(
    idle_runtime, monkeypatch: pytest.MonkeyPatch, resource: str
) -> None:
    runtime = idle_runtime

    async def broken():
        raise RuntimeError("inventory unavailable")

    owner = {
        "browser": runtime.browser,
        "receiver": runtime.peer_receiver,
        "probe": runtime.peer_probe,
    }[resource]
    monkeypatch.setattr(owner, "has_active_resources", broken)
    with pytest.raises(RuntimeError, match="inventory unavailable"):
        await runtime.require_source_cutover_idle()
