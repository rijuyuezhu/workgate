import hashlib
import os
from pathlib import Path

import pytest

from tests.e2e_helpers import (
    run_http_process_with_executors,
    streamable_http_tool_client,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


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
            },
        )
        assert directory_copy["transport"] == "same_executor"
        assert directory_copy["relation"]["route"] == "same_executor"
        assert directory_copy["chunks"] > 1
        assert (
            workspace / "destination-directory" / "payload.bin"
        ).read_bytes() == directory_payload


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
            },
        )
        assert copied["transport"] == "executor_rpc"
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
            },
        )
        assert directory_copy["transport"] == "executor_rpc"
        assert directory_copy["relation"]["route"] == "different_executors"
        assert directory_copy["chunks"] > 1
        assert (
            destination_workspace / "destination-directory" / "payload.bin"
        ).read_bytes() == directory_payload

        control_workspace = tmp_path / "control-workspace-mcp"
        assert not (control_workspace / "source.bin").exists()
        assert not (control_workspace / "destination.bin").exists()
