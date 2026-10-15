"""Exercise the private receiver through the actual Executor command boundary."""

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
