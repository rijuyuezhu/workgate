import hashlib
import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.e2e_helpers import (
    run_http_process_with_executors,
    streamable_http_tool_client,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class _LocalS3State:
    def __init__(self) -> None:
        self.endpoint_url = ""
        self.objects: dict[str, bytes] = {}
        self.puts = 0
        self.gets = 0
        self.deletes = 0
        self.lock = threading.Lock()


@contextmanager
def _local_s3_server():
    state = _LocalS3State()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args) -> None:
            _ = (format, args)

        def _key(self) -> str:
            return urlsplit(self.path).path

        def do_PUT(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            data = self.rfile.read(length)
            with state.lock:
                state.objects[self._key()] = data
                state.puts += 1
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            key = self._key()
            with state.lock:
                data = state.objects.get(key)
                state.gets += 1
            if data is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            range_header = self.headers.get("range")
            if range_header:
                assert range_header.startswith("bytes=")
                start = int(
                    range_header.removeprefix("bytes=").removesuffix("-")
                )
                if start >= len(data):
                    self.send_response(416)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = data[start:]
                self.send_response(206)
                self.send_header(
                    "Content-Range",
                    f"bytes {start}-{len(data) - 1}/{len(data)}",
                )
            else:
                body = data
                self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self) -> None:
            with state.lock:
                state.objects.pop(self._key(), None)
                state.deletes += 1
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.endpoint_url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def _start_session(client, *, executor_id: str) -> dict:
    return await client.call_tool(
        "session_start",
        {"executor_id": executor_id, "workdir": "."},
    )


async def test_real_same_executor_large_session_copy(tmp_path: Path) -> None:
    payload = (b"same-executor-transfer-" * 60000) + b"tail"
    assert len(payload) > 1024 * 1024
    workspace = tmp_path / "executor-one"

    async with (
        run_http_process_with_executors(
            tmp_path,
            mode="mcp",
            executor_workspaces=(workspace,),
        ) as (base_url, executors),
        streamable_http_tool_client(base_url) as client,
    ):
        executor = executors[0]
        source = await _start_session(client, executor_id=executor.executor_id)
        destination = await _start_session(
            client, executor_id=executor.executor_id
        )
        assert source["session_id"] != destination["session_id"]

        (workspace / "source.bin").write_bytes(payload)
        copied = await client.call_tool(
            "session_copy",
            {
                "src_session_id": source["session_id"],
                "src_path": "source.bin",
                "dst_session_id": destination["session_id"],
                "dst_path": "destination.bin",
                "kind": "file",
                "chunk_size": 64 * 1024,
                "background": False,
            },
        )
        assert copied["transport"] == "same_executor"
        assert copied["relation"]["route"] == "same_executor"
        assert copied["relation"]["same_executor"] is True
        assert copied["source"]["session_id"] == source["session_id"]
        assert copied["destination"]["session_id"] == destination["session_id"]
        assert copied["source"]["executor_id"] == executor.executor_id
        assert copied["destination"]["executor_id"] == executor.executor_id
        assert "target" not in copied["source"]
        assert "target" not in copied["destination"]
        assert copied["bytes"] == len(payload)
        assert copied["sha256"] == hashlib.sha256(payload).hexdigest()
        assert copied["chunks"] > 1
        assert (workspace / "destination.bin").read_bytes() == payload

        directory_payload = os.urandom(1_100_000)
        source_directory = workspace / "source-directory"
        source_directory.mkdir()
        (source_directory / "payload.bin").write_bytes(directory_payload)
        directory_copy = await client.call_tool(
            "session_copy",
            {
                "src_session_id": source["session_id"],
                "src_path": "source-directory",
                "dst_session_id": destination["session_id"],
                "dst_path": "destination-directory",
                "kind": "dir",
                "chunk_size": 64 * 1024,
                "background": False,
            },
        )
        assert directory_copy["transport"] == "same_executor"
        assert directory_copy["relation"]["route"] == "same_executor"
        assert directory_copy["chunks"] > 1
        assert (
            workspace / "destination-directory" / "payload.bin"
        ).read_bytes() == directory_payload


@pytest.mark.topology
async def test_real_two_executors_object_store_large_session_copy(
    tmp_path: Path,
) -> None:
    pytest.importorskip("boto3")
    payload = (b"object-store-transfer-" * 70000) + b"done"
    assert len(payload) > 1024 * 1024
    source_workspace = tmp_path / "object-source"
    destination_workspace = tmp_path / "object-destination"

    with _local_s3_server() as object_store:
        async with (
            run_http_process_with_executors(
                tmp_path,
                mode="mcp",
                executor_workspaces=(source_workspace, destination_workspace),
                control_overrides={
                    "transfer_object_store_bucket": "workgate-e2e",
                    "transfer_object_store_prefix": "integration",
                    "transfer_object_store_region": "us-east-1",
                    "transfer_object_store_endpoint_url": (
                        object_store.endpoint_url
                    ),
                    "transfer_object_store_presign_ttl_s": 300,
                },
                control_env_overrides={
                    "AWS_ACCESS_KEY_ID": "workgate-test",
                    "AWS_SECRET_ACCESS_KEY": "workgate-test-secret",
                    "AWS_EC2_METADATA_DISABLED": "true",
                },
            ) as (base_url, executors),
            streamable_http_tool_client(base_url) as client,
        ):
            source_executor, destination_executor = executors
            source = await _start_session(
                client, executor_id=source_executor.executor_id
            )
            destination = await _start_session(
                client, executor_id=destination_executor.executor_id
            )

            (source_workspace / "source.bin").write_bytes(payload)
            copied = await client.call_tool(
                "session_copy",
                {
                    "src_session_id": source["session_id"],
                    "src_path": "source.bin",
                    "dst_session_id": destination["session_id"],
                    "dst_path": "destination.bin",
                    "kind": "file",
                    "chunk_size": 64 * 1024,
                    "background": False,
                },
            )
            assert copied["transport"] == "object_store"
            assert copied["fallbacks"] == []
            assert copied["bytes"] == len(payload)
            assert copied["sha256"] == hashlib.sha256(payload).hexdigest()
            assert (
                destination_workspace / "destination.bin"
            ).read_bytes() == payload
            with object_store.lock:
                assert object_store.objects == {}

            directory_payload = os.urandom(1_100_000)
            source_directory = source_workspace / "source-directory"
            source_directory.mkdir()
            (source_directory / "payload.bin").write_bytes(directory_payload)
            directory_copy = await client.call_tool(
                "session_copy",
                {
                    "src_session_id": source["session_id"],
                    "src_path": "source-directory",
                    "dst_session_id": destination["session_id"],
                    "dst_path": "destination-directory",
                    "kind": "dir",
                    "chunk_size": 64 * 1024,
                    "background": False,
                },
            )
            assert directory_copy["transport"] == "object_store"
            assert directory_copy["fallbacks"] == []
            assert directory_copy["chunks"] > 1
            assert (
                destination_workspace / "destination-directory" / "payload.bin"
            ).read_bytes() == directory_payload
            with object_store.lock:
                assert object_store.objects == {}
                assert object_store.puts == 2
                assert object_store.gets >= 2
                assert object_store.deletes == 2

            control_payload_dir = (
                tmp_path
                / "control-data-mcp"
                / "control"
                / "payloads"
                / "transfer"
            )
            assert not list(control_payload_dir.glob("payload_*.bin"))


async def test_real_two_executors_large_session_copy(tmp_path: Path) -> None:
    payload = (b"cross-executor-transfer-" * 70000) + b"done"
    assert len(payload) > 1024 * 1024
    source_workspace = tmp_path / "executor-source"
    destination_workspace = tmp_path / "executor-destination"

    async with (
        run_http_process_with_executors(
            tmp_path,
            mode="mcp",
            executor_workspaces=(source_workspace, destination_workspace),
        ) as (base_url, executors),
        streamable_http_tool_client(base_url) as client,
    ):
        source_executor, destination_executor = executors
        source = await _start_session(
            client, executor_id=source_executor.executor_id
        )
        destination = await _start_session(
            client, executor_id=destination_executor.executor_id
        )
        assert source["session_id"] != destination["session_id"]

        (source_workspace / "source.bin").write_bytes(payload)
        copied = await client.call_tool(
            "session_copy",
            {
                "src_session_id": source["session_id"],
                "src_path": "source.bin",
                "dst_session_id": destination["session_id"],
                "dst_path": "destination.bin",
                "kind": "file",
                "chunk_size": 64 * 1024,
                "background": False,
            },
        )
        assert copied["transport"] == "control_relay"
        assert copied["relation"]["route"] == "different_executors"
        assert copied["relation"]["same_executor"] is False
        assert copied["source"]["executor_id"] == source_executor.executor_id
        assert (
            copied["destination"]["executor_id"]
            == destination_executor.executor_id
        )
        assert copied["bytes"] == len(payload)
        assert copied["sha256"] == hashlib.sha256(payload).hexdigest()
        assert copied["chunks"] > 1
        assert (
            destination_workspace / "destination.bin"
        ).read_bytes() == payload

        directory_payload = os.urandom(1_100_000)
        source_directory = source_workspace / "source-directory"
        source_directory.mkdir()
        (source_directory / "payload.bin").write_bytes(directory_payload)
        directory_copy = await client.call_tool(
            "session_copy",
            {
                "src_session_id": source["session_id"],
                "src_path": "source-directory",
                "dst_session_id": destination["session_id"],
                "dst_path": "destination-directory",
                "kind": "dir",
                "chunk_size": 64 * 1024,
                "background": False,
            },
        )
        assert directory_copy["transport"] == "control_relay"
        assert directory_copy["relation"]["route"] == "different_executors"
        assert directory_copy["chunks"] > 1
        assert (
            destination_workspace / "destination-directory" / "payload.bin"
        ).read_bytes() == directory_payload

        control_workspace = tmp_path / "control-workspace-mcp"
        assert not (control_workspace / "source.bin").exists()
        assert not (control_workspace / "destination.bin").exists()
