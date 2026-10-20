"""Real Windows Control -> Executor -> WebUI WebSocket ConPTY validation."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("Run on native Windows CPython")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import httpx  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402
from websockets.typing import Subprotocol  # noqa: E402

from tests.e2e.helpers import (  # noqa: E402
    free_tcp_port,
    isolated_xdg_env,
    provision_executor_pair,
    start_control_process,
    start_executor_process,
    stop_process,
    wait_for_executor_online,
    wait_for_http_ready,
    write_yaml_config,
)


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="wg-conpty-network-") as temp:
        root = Path(temp)
        port = free_tcp_port()
        base_url = f"http://127.0.0.1:{port}"
        logs = root / "logs"
        workspace = root / "workspace"
        workspace.mkdir()
        state = root / "control-state"
        executor_state = root / "executor-state"
        env = isolated_xdg_env(root / "xdg")
        configs = root / "configs"
        executor_id = provision_executor_pair(
            control_state_dir=state,
            executor_state_dir=executor_state,
            control_url=base_url,
        )
        control_conf = write_yaml_config(
            configs / "control.yaml",
            {
                "mode": "http",
                "host": "127.0.0.1",
                "port": port,
                "base_url": base_url,
                "auth_mode": "none",
                "state_dir": str(state),
                "data_dir": str(root / "control-data"),
                "agent_bridge_enabled": False,
            },
        )
        executor_conf = write_yaml_config(
            configs / "executor.yaml",
            {
                "default_workdir": str(workspace),
                "state_dir": str(executor_state),
                "shell_executable": "powershell.exe",
            },
        )
        control = start_control_process(
            control_conf,
            env=env,
            stdout_path=logs / "control.out",
            stderr_path=logs / "control.err",
        )
        executor = None
        try:
            await wait_for_http_ready(
                base_url,
                control,
                stdout_path=logs / "control.out",
                stderr_path=logs / "control.err",
                timeout_s=35,
            )
            executor = start_executor_process(
                executor_conf,
                env=env,
                stdout_path=logs / "executor.out",
                stderr_path=logs / "executor.err",
            )
            await wait_for_executor_online(
                base_url,
                executor,
                executor_id,
                stdout_path=logs / "executor.out",
                stderr_path=logs / "executor.err",
                timeout_s=40,
            )
            async with httpx.AsyncClient(
                base_url=base_url, timeout=40, trust_env=False
            ) as client:
                started = await client.post(
                    "/api/ui/terminals/start",
                    json={
                        "executor_id": executor_id,
                        "name": "native-win",
                        "cwd": ".",
                    },
                )
                started.raise_for_status()
                shell_id = started.json()["data"]["shell_id"]
                try:

                    async def attach():
                        response = await client.post(
                            "/api/ui/terminals/attach",
                            json={
                                "executor_id": executor_id,
                                "shell_id": shell_id,
                                "cols": 100,
                                "rows": 30,
                            },
                        )
                        response.raise_for_status()
                        grant = response.json()["data"]
                        assert grant["backend"] == "conpty", grant
                        return grant

                    async def connect_terminal(grant):
                        return connect(
                            f"ws://127.0.0.1:{port}/stream/{grant['stream_id']}",
                            subprotocols=[
                                Subprotocol("workgate-ui-terminal"),
                                Subprotocol(
                                    f"workgate-stream-token.{grant['browser_token']}"
                                ),
                            ],
                            proxy=None,
                        )

                    async def await_output(socket, expected):
                        output = b""
                        async with asyncio.timeout(25):
                            while expected not in output:
                                frame = await socket.recv()
                                if isinstance(frame, bytes):
                                    output += frame

                    first, second = await attach(), await attach()
                    first_connection = await connect_terminal(first)
                    second_connection = await connect_terminal(second)
                    async with (
                        first_connection as ws_a,
                        second_connection as ws_b,
                    ):
                        for websocket in (ws_a, ws_b):
                            ready = json.loads(
                                await asyncio.wait_for(websocket.recv(), 15)
                            )
                            assert ready["type"] == "ready", ready
                        # The expected output is not a literal substring of
                        # the command, so an echoed input cannot falsely pass.
                        await ws_a.send(
                            b"Write-Output ('ACTUAL_'+'WIN_MULTI_386')\r"
                        )
                        await asyncio.gather(
                            await_output(ws_a, b"ACTUAL_WIN_MULTI_386"),
                            await_output(ws_b, b"ACTUAL_WIN_MULTI_386"),
                        )
                        await ws_a.close()
                        await ws_b.send(
                            b"Write-Output ('AFTER_'+'DETACH_386')\r"
                        )
                        await await_output(ws_b, b"AFTER_DETACH_386")
                finally:
                    killed = await client.post(
                        "/api/ui/terminals/kill",
                        json={"executor_id": executor_id, "shell_id": shell_id},
                    )
                    killed.raise_for_status()
            print(
                "Native Windows Control/Executor, two WebSocket clients and detach: PASS"
            )
        finally:
            if executor is not None:
                stop_process(executor)
            stop_process(control)
            # The Windows Parent's Job Object kills its Child on close, but
            # the Child may release its locked state file just after Parent exits.
            lock = state / "control" / "run.lock"
            for attempt in range(100):
                try:
                    lock.unlink(missing_ok=True)
                    break
                except PermissionError:
                    if attempt == 99:
                        raise
                    await asyncio.sleep(0.05)


if __name__ == "__main__":
    asyncio.run(main())
