"""Exercise the private receiver through the actual Executor command boundary."""

import asyncio
import hashlib

import pytest

import workgate.executor.peer_receiver as peer_module
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor.runtime import build_executor_runtime
from workgate.protocol.executor import (
    SESSION_CHANGE_WORKDIR_OP,
    SESSION_CREATE_OP,
    SESSION_TERMINATE_OP,
    ExecutorCommand,
    TrustedOverlay,
)
from workgate.protocol.ids import new_command_id, new_session_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lifecycle_op", [SESSION_CHANGE_WORKDIR_OP, SESSION_TERMINATE_OP]
)
async def test_peer_receiver_is_closed_before_session_lifecycle_change(
    tmp_path, monkeypatch, lifecycle_op
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                default_workdir=workspace,
                state_dir=tmp_path / "state",
                agent_bridge_enabled=False,
            )
        ),
        enable_control_connection=False,
    )
    monkeypatch.setattr(
        peer_module,
        "detect_tailscale_overlay",
        lambda: TrustedOverlay.model_construct(
            provider="tailscale", tailnet_id="tailnet-test", ipv4="127.0.0.1"
        ),
    )
    session_id = new_session_id()

    def command(op, args):
        return ExecutorCommand(
            id=new_command_id(), op=op, session_id=session_id, args=args
        )

    await runtime.start()
    try:
        await runtime._execute_protocol_command(
            command(SESSION_CREATE_OP, {"workdir": str(workspace)})
        )
        transfer_id = "copy_" + "r" * 22
        prep = await runtime._execute_protocol_command(
            command(
                "transfer.peer_prepare",
                {
                    "path": "destination.bin",
                    "transfer_id": transfer_id,
                    "expected_bytes": 3,
                    "expected_sha256": hashlib.sha256(b"abc").hexdigest(),
                    "overwrite": True,
                    "tailnet_id": "tailnet-test",
                },
            )
        )
        assert isinstance(prep, dict)
        assert prep["token"]
        assert prep["host"] == "127.0.0.1"
        assert runtime.peer_receiver.status(transfer_id) == "pending"
        await runtime._execute_protocol_command(
            command(
                lifecycle_op,
                {"workdir": str(other)}
                if lifecycle_op == SESSION_CHANGE_WORKDIR_OP
                else {},
            )
        )
        assert runtime.peer_receiver.status(transfer_id) == "unknown"
        assert not (workspace / "destination.bin").exists()
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_termination_serializes_with_peer_prepare(tmp_path, monkeypatch):
    """A newly prepared receiver must not survive Session termination."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                default_workdir=workspace,
                state_dir=tmp_path / "state",
                agent_bridge_enabled=False,
            )
        ),
        enable_control_connection=False,
    )
    monkeypatch.setattr(
        peer_module,
        "detect_tailscale_overlay",
        lambda: TrustedOverlay.model_construct(
            provider="tailscale", tailnet_id="tailnet-test", ipv4="127.0.0.1"
        ),
    )
    sid = new_session_id()
    runtime.services.tool_session_store.create_session(
        session_id=str(sid), workdir=workspace
    )
    entered, release = asyncio.Event(), asyncio.Event()
    terminate = runtime.sessions.terminate

    async def delayed_terminate(session_id):
        entered.set()
        await release.wait()
        return await terminate(session_id)

    monkeypatch.setattr(runtime.sessions, "terminate", delayed_terminate)
    terminating = asyncio.create_task(
        runtime._execute_protocol_command(
            ExecutorCommand(
                id=new_command_id(), op=SESSION_TERMINATE_OP, session_id=sid
            )
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        prepare = asyncio.create_task(
            runtime._execute_protocol_command(
                ExecutorCommand(
                    id=new_command_id(),
                    op="transfer.peer_prepare",
                    session_id=sid,
                    args={
                        "path": "orphan.bin",
                        "transfer_id": "copy_" + "j" * 22,
                        "expected_bytes": 1,
                        "expected_sha256": hashlib.sha256(b"x").hexdigest(),
                        "overwrite": True,
                        "tailnet_id": "tailnet-test",
                    },
                )
            )
        )
        await asyncio.sleep(0.05)
        assert not prepare.done()
        release.set()
        await terminating
        with pytest.raises((ValueError, RuntimeError)):
            await prepare
        assert runtime.peer_receiver.status("copy_" + "j" * 22) == "unknown"
    finally:
        release.set()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_peer_runtime_status_cancel_bound_to_session(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                default_workdir=workspace,
                state_dir=tmp_path / "state",
                agent_bridge_enabled=False,
            )
        ),
        enable_control_connection=False,
    )
    monkeypatch.setattr(
        peer_module,
        "detect_tailscale_overlay",
        lambda: TrustedOverlay.model_construct(
            provider="tailscale", tailnet_id="tailnet-test", ipv4="127.0.0.1"
        ),
    )
    sid = new_session_id()
    other = new_session_id()
    for session in (sid, other):
        runtime.services.tool_session_store.create_session(
            session_id=str(session), workdir=workspace
        )
    tid = "copy_" + "k" * 22

    async def call(op, session_id, args):
        return await runtime._execute_protocol_command(
            ExecutorCommand(
                id=new_command_id(),
                op=op,
                session_id=session_id,
                args=args,
            )
        )

    try:
        await call(
            "transfer.peer_prepare",
            sid,
            {
                "path": "ready.bin",
                "transfer_id": tid,
                "expected_bytes": 0,
                "expected_sha256": hashlib.sha256(b"").hexdigest(),
                "overwrite": True,
                "tailnet_id": "tailnet-test",
            },
        )
        assert await call(
            "transfer.peer_status", sid, {"transfer_id": tid}
        ) == {"state": "pending"}
        assert await call(
            "transfer.peer_status", other, {"transfer_id": tid}
        ) == {"state": "unknown"}
        assert await call(
            "transfer.peer_cancel", other, {"transfer_id": tid}
        ) == {"cancelled": False}
        for op in ("transfer.peer_status", "transfer.peer_cancel"):
            with pytest.raises(ValueError, match="destination session"):
                await call(op, None, {"transfer_id": tid})
        assert await call(
            "transfer.peer_cancel", sid, {"transfer_id": tid}
        ) == {"cancelled": True}
        assert await call(
            "transfer.peer_cancel", sid, {"transfer_id": tid}
        ) == {"cancelled": False}
        with pytest.raises(ValueError, match="destination session"):
            await call("transfer.peer_prepare", None, {})
    finally:
        await runtime.aclose()
