import base64
import hashlib
from typing import Any, cast

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

import workgate.ops.image as image_ops
from workgate.config.settings import clear_settings_cache
from workgate.control.mcp.app import build_mcp
from workgate.tool_session.store import get_tool_session_store

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lP7LAAAAAElFTkSuQmCC"
)


def _configure(tmp_path, monkeypatch, *, max_bytes: int | None = None) -> None:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    if max_bytes is not None:
        monkeypatch.setenv("WORKGATE_MAX_VIEW_IMAGE_BYTES", str(max_bytes))
    clear_settings_cache()
    get_tool_session_store().clear()


def _local_session(tmp_path) -> str:
    return get_tool_session_store().create_session(workdir=tmp_path).session_id


def test_detect_image_type_supports_common_web_formats():
    assert image_ops.detect_image_type(PNG_BYTES[:16]) == ("png", "image/png")
    assert image_ops.detect_image_type(b"\xff\xd8\xff\xe0rest") == (
        "jpeg",
        "image/jpeg",
    )
    assert image_ops.detect_image_type(b"GIF89a-rest") == ("gif", "image/gif")
    assert image_ops.detect_image_type(b"RIFF1234WEBPrest") == (
        "webp",
        "image/webp",
    )
    with pytest.raises(ValueError, match="Unsupported image format"):
        image_ops.detect_image_type(b"plain text")


@pytest.mark.asyncio
async def test_local_image_is_session_bound_and_bounded(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    image_path = tmp_path / "pixel.png"
    image_path.write_bytes(PNG_BYTES)
    session_id = _local_session(tmp_path)

    image = await image_ops.read_image_dispatch_execute("pixel.png", session_id)

    assert image.data == PNG_BYTES
    assert image.mime_type == "image/png"
    assert image.size == len(PNG_BYTES)
    assert image.path.endswith("pixel.png")

    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(PNG_BYTES)
    with pytest.raises(ValueError, match="escapes (session workdir|workspace)"):
        await image_ops.read_image_dispatch_execute(
            "../outside.png", session_id
        )


@pytest.mark.asyncio
async def test_local_image_rejects_empty_oversized_and_unsupported(
    tmp_path, monkeypatch
):
    _configure(tmp_path, monkeypatch, max_bytes=len(PNG_BYTES) - 1)
    session_id = _local_session(tmp_path)
    (tmp_path / "large.png").write_bytes(PNG_BYTES)
    (tmp_path / "empty.png").write_bytes(b"")
    (tmp_path / "fake.png").write_bytes(b"not an image")

    with pytest.raises(ValueError, match="max is"):
        await image_ops.read_image_dispatch_execute("large.png", session_id)

    monkeypatch.setenv("WORKGATE_MAX_VIEW_IMAGE_BYTES", "1024")
    clear_settings_cache()
    with pytest.raises(ValueError, match="empty"):
        await image_ops.read_image_dispatch_execute("empty.png", session_id)
    with pytest.raises(ValueError, match="Unsupported image format"):
        await image_ops.read_image_dispatch_execute("fake.png", session_id)


def _chunk(data: bytes, *, offset: int = 0, size: int | None = None) -> Any:
    return image_ops.TransferReadChunkOutput(
        path="pixel.png",
        offset=offset,
        bytes=len(data),
        size=len(data) if size is None else size,
        eof=True,
        sha256=hashlib.sha256(data).hexdigest(),
        data_b64=base64.b64encode(data).decode("ascii"),
    )


def test_remote_chunk_validation_rejects_corruption():
    valid = _chunk(PNG_BYTES)
    assert (
        image_ops._decode_remote_chunk(
            valid, expected_offset=0, expected_size=len(PNG_BYTES)
        )
        == PNG_BYTES
    )

    with pytest.raises(RuntimeError, match="offset mismatch"):
        image_ops._decode_remote_chunk(
            valid, expected_offset=1, expected_size=len(PNG_BYTES)
        )

    with pytest.raises(RuntimeError, match="size changed"):
        image_ops._decode_remote_chunk(
            valid, expected_offset=0, expected_size=len(PNG_BYTES) + 1
        )

    invalid_base64 = valid.model_copy(update={"data_b64": "%%%"})
    with pytest.raises(RuntimeError, match="valid base64"):
        image_ops._decode_remote_chunk(
            invalid_base64,
            expected_offset=0,
            expected_size=len(PNG_BYTES),
        )

    invalid_length = valid.model_copy(update={"bytes": len(PNG_BYTES) + 1})
    with pytest.raises(RuntimeError, match="length mismatch"):
        image_ops._decode_remote_chunk(
            invalid_length,
            expected_offset=0,
            expected_size=len(PNG_BYTES),
        )

    invalid_digest = valid.model_copy(update={"sha256": "0" * 64})
    with pytest.raises(RuntimeError, match="SHA-256"):
        image_ops._decode_remote_chunk(
            invalid_digest,
            expected_offset=0,
            expected_size=len(PNG_BYTES),
        )


@pytest.mark.asyncio
async def test_remote_image_reuses_transfer_protocol(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    store = get_tool_session_store()
    session = store.create_session(
        target="remote",
        machine="worker-a",
        workdir="/remote/work",
        worker_session_id="WORKER12",
    )
    split = len(PNG_BYTES) // 2
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_remote(_session, tool: str, args: dict[str, Any]):
        calls.append((tool, args))
        if tool == "transfer_stat":
            return {
                "path": "pixel.png",
                "type": "file",
                "size": len(PNG_BYTES),
                "modified": 1.0,
                "sha256": None,
            }
        offset = int(args["offset"])
        data = PNG_BYTES[:split] if offset == 0 else PNG_BYTES[split:]
        return {
            "path": "pixel.png",
            "offset": offset,
            "bytes": len(data),
            "size": len(PNG_BYTES),
            "eof": offset + len(data) == len(PNG_BYTES),
            "sha256": hashlib.sha256(data).hexdigest(),
            "data_b64": base64.b64encode(data).decode("ascii"),
        }

    monkeypatch.setattr(image_ops, "call_remote_session_tool", fake_remote)
    monkeypatch.setattr(image_ops, "DEFAULT_TRANSFER_CHUNK_BYTES", split)

    result = await image_ops.view_image_dispatch_execute(
        "pixel.png", session.session_id
    )

    assert isinstance(result.content[0], ImageContent)
    assert base64.b64decode(result.content[0].data) == PNG_BYTES
    assert isinstance(result.content[1], TextContent)
    assert result.structuredContent == {
        "session_id": session.session_id,
        "target": "remote",
        "machine": "worker-a",
        "path": "pixel.png",
        "mime_type": "image/png",
        "bytes": len(PNG_BYTES),
    }
    assert [tool for tool, _args in calls] == [
        "transfer_stat",
        "transfer_read_chunk",
        "transfer_read_chunk",
    ]


@pytest.mark.asyncio
async def test_view_image_tool_returns_native_mcp_content(
    tmp_path, monkeypatch
):
    _configure(tmp_path, monkeypatch)
    (tmp_path / "pixel.png").write_bytes(PNG_BYTES)
    session_id = _local_session(tmp_path)
    mcp = build_mcp()
    tools = {tool.name: tool for tool in await mcp.list_tools()}

    assert "view_image" in tools
    assert set(tools["view_image"].inputSchema["required"]) == {
        "session_id",
        "path",
    }
    response = cast(
        CallToolResult,
        await mcp.call_tool(
            "view_image", {"session_id": session_id, "path": "pixel.png"}
        ),
    )

    assert isinstance(response.content[0], ImageContent)
    assert base64.b64decode(response.content[0].data) == PNG_BYTES
    assert isinstance(response.content[1], TextContent)
    assert response.structuredContent is not None
    assert response.structuredContent["target"] == "local"
    assert response.structuredContent["mime_type"] == "image/png"
    assert response.structuredContent["bytes"] == len(PNG_BYTES)
