"""Real Control/Executor transport for WebUI terminals across two runtimes."""

import asyncio
import json
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from websockets.asyncio.client import connect
from websockets.typing import Subprotocol

from tests.e2e.helpers import (
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

pytestmark = [pytest.mark.integration, pytest.mark.topology]


def _tls_certificate(directory: Path) -> tuple[Path, Path]:
    """Short-lived localhost trust root for a production-style HTTPS endpoint."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
        .sign(key, hashes.SHA256())
    )
    cert_file = directory / "localhost.crt"
    key_file = directory / "localhost.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    return cert_file, key_file


async def _forward_tls_to_control(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    port: int,
) -> None:
    """Transparent TLS terminator, including WebSocket Upgrade byte streams."""
    backend_reader, backend_writer = await asyncio.open_connection(
        "127.0.0.1", port
    )

    async def copy(
        source: asyncio.StreamReader, target: asyncio.StreamWriter
    ) -> None:
        while data := await source.read(65536):
            target.write(data)
            await target.drain()

    tasks = [
        asyncio.create_task(copy(reader, backend_writer)),
        asyncio.create_task(copy(backend_reader, writer)),
    ]
    try:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for task in done:
            task.result()
    finally:
        writer.close()
        backend_writer.close()


async def _attach_and_execute(
    client: httpx.AsyncClient,
    base_url: str,
    executor_id: str,
    marker: str,
) -> None:
    start = await client.post(
        "/api/ui/terminals/start",
        json={
            "executor_id": executor_id,
            "name": f"stream-{marker}",
            "cwd": ".",
        },
    )
    start.raise_for_status()
    shell_id = start.json()["data"]["shell_id"]
    try:
        attached = await client.post(
            "/api/ui/terminals/attach",
            json={
                "executor_id": executor_id,
                "shell_id": shell_id,
                "cols": 100,
                "rows": 30,
            },
        )
        attached.raise_for_status()
        grant = attached.json()["data"]
        assert grant["executor_id"] == executor_id
        assert grant["shell_id"] == shell_id
        assert grant["mode"] == "pty"
        async with connect(
            f"{base_url.replace('http:', 'ws:')}/stream/{grant['stream_id']}",
            subprotocols=[
                Subprotocol("workgate-ui-terminal"),
                Subprotocol(f"workgate-stream-token.{grant['browser_token']}"),
            ],
            proxy=None,
        ) as websocket:
            ready = json.loads(await asyncio.wait_for(websocket.recv(), 10))
            assert ready["type"] == "ready"
            assert ready["executor_id"] == executor_id
            await websocket.send(b"cat identity-marker.txt\r")
            output = b""
            async with asyncio.timeout(10):
                while marker.encode() not in output:
                    message = await websocket.recv()
                    if isinstance(message, bytes):
                        output += message
            assert marker.encode() in output
    finally:
        killed = await client.post(
            "/api/ui/terminals/kill",
            json={"executor_id": executor_id, "shell_id": shell_id},
        )
        killed.raise_for_status()


@pytest.mark.asyncio
async def test_control_terminal_stream_on_two_distinct_executor_processes(
    tmp_path: Path,
) -> None:
    port = free_tcp_port()
    tls_port = free_tcp_port()
    base_url = f"http://127.0.0.1:{port}"
    tls_url = f"https://localhost:{tls_port}"
    state = tmp_path / "control-state"
    data = tmp_path / "control-data"
    configs = tmp_path / "configs"
    logs = tmp_path / "logs"
    env = isolated_xdg_env(tmp_path / "xdg")
    cert_file, key_file = _tls_certificate(tmp_path)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    # Exercise the real HTTPX and websockets proxy bypass behavior too.
    env.update(
        {
            "SSL_CERT_FILE": str(cert_file),
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "localhost,127.0.0.1",
            "no_proxy": "localhost,127.0.0.1",
        }
    )
    control_config = write_yaml_config(
        configs / "control.yaml",
        {
            "mode": "http",
            "host": "127.0.0.1",
            "port": port,
            "base_url": base_url,
            "auth_mode": "none",
            "state_dir": str(state),
            "data_dir": str(data),
            "agent_bridge_enabled": False,
        },
    )
    executor_ids = []
    executor_configs = []
    for name in ("control-side", "separate-executor"):
        workspace = tmp_path / name
        workspace.mkdir()
        (workspace / "identity-marker.txt").write_text(
            f"running-on-{name}\n", encoding="utf-8"
        )
        executor_state = tmp_path / f"state-{name}"
        executor_ids.append(
            provision_executor_pair(
                control_state_dir=state,
                executor_state_dir=executor_state,
                control_url=tls_url
                if name == "separate-executor"
                else base_url,
                name=name,
            )
        )
        executor_configs.append(
            write_yaml_config(
                configs / f"{name}.yaml",
                {
                    "default_workdir": str(workspace),
                    "state_dir": str(executor_state),
                },
            )
        )
    control = start_control_process(
        control_config,
        env=env,
        stdout_path=logs / "control.out",
        stderr_path=logs / "control.err",
    )
    executors = []
    try:
        await wait_for_http_ready(base_url, control)
        server = await asyncio.start_server(
            lambda reader, writer: _forward_tls_to_control(
                reader, writer, port
            ),
            "127.0.0.1",
            tls_port,
            ssl=tls,
        )
        async with server:
            try:
                await _test_both_executors(
                    base_url,
                    env,
                    logs,
                    executor_configs,
                    executor_ids,
                    executors,
                )
            finally:
                # Close long-lived HTTPS poll sockets before TLS server shutdown.
                for executor in executors:
                    stop_process(executor)
    finally:
        stop_process(control)


async def _test_both_executors(
    base_url: str,
    env: dict[str, str],
    logs: Path,
    executor_configs: list[Path],
    executor_ids: list[str],
    executors: list,
) -> None:
    for index, config in enumerate(executor_configs):
        executor = start_executor_process(
            config,
            env=env,
            stdout_path=logs / f"executor{index}.out",
            stderr_path=logs / f"executor{index}.err",
        )
        executors.append(executor)
        await wait_for_executor_online(base_url, executor, executor_ids[index])
    async with httpx.AsyncClient(
        base_url=base_url, timeout=20, trust_env=False
    ) as client:
        for name, executor_id in zip(
            ("control-side", "separate-executor"), executor_ids, strict=True
        ):
            await _attach_and_execute(
                client, base_url, executor_id, f"running-on-{name}"
            )
