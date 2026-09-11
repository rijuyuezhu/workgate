import base64
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

import workgate.executor.image as image_ops
from tests.helpers import build_paired_control_harness, mcp_structured
from workgate.config.settings import (
    Settings,
    clear_settings_cache,
    get_settings,
)
from workgate.control.mcp.app import build_mcp
from workgate.executor.config import ExecutorConfig, resolve_executor_config
from workgate.executor.tool_session.store import ToolSessionStore
from workgate.persistence import FileStateStore
from workgate.utils.image_types import detect_image_type

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lP7LAAAAAElFTkSuQmCC"
)


def _executor(
    tmp_path: Path, *, max_bytes: int | None = None
) -> tuple[ExecutorConfig, ToolSessionStore, str]:
    settings = Settings(
        workspace_root=tmp_path,
        state_dir=tmp_path / ".state",
        agent_bridge_enabled=False,
        max_view_image_bytes=(
            20 * 1024 * 1024 if max_bytes is None else max_bytes
        ),
    )
    config = resolve_executor_config(settings)
    config.workspace_root.mkdir(parents=True, exist_ok=True)
    store = ToolSessionStore(
        FileStateStore(lambda: settings.state_dir),
        settings_provider=lambda: settings,
    )
    session_id = "sess_0000000000000000000001"
    store.create_session(session_id=session_id, workdir=config.workspace_root)
    return config, store, session_id


def test_detect_image_type_supports_common_web_formats():
    assert detect_image_type(PNG_BYTES[:16]) == ("png", "image/png")
    assert detect_image_type(b"\xff\xd8\xff\xe0rest") == (
        "jpeg",
        "image/jpeg",
    )
    assert detect_image_type(b"GIF89a-rest") == ("gif", "image/gif")
    assert detect_image_type(b"RIFF1234WEBPrest") == (
        "webp",
        "image/webp",
    )
    with pytest.raises(ValueError, match="Unsupported image format"):
        detect_image_type(b"plain text")


@pytest.mark.asyncio
async def test_executor_image_is_session_bound_and_bounded(tmp_path: Path):
    config, store, session_id = _executor(tmp_path)
    image_path = config.workspace_root / "pixel.png"
    image_path.write_bytes(PNG_BYTES)

    session, image = await image_ops.read_image_execute(
        config, store, "pixel.png", session_id
    )

    assert session.session_id == session_id
    assert image.data == PNG_BYTES
    assert image.mime_type == "image/png"
    assert image.size == len(PNG_BYTES)
    assert image.path == "pixel.png"

    outside = config.workspace_root.parent / "outside.png"
    outside.write_bytes(PNG_BYTES)
    with pytest.raises(ValueError, match="escapes (session workdir|workspace)"):
        await image_ops.read_image_execute(
            config, store, "../outside.png", session_id
        )


@pytest.mark.asyncio
async def test_executor_image_rejects_empty_oversized_and_unsupported(
    tmp_path: Path,
):
    config, store, session_id = _executor(
        tmp_path, max_bytes=len(PNG_BYTES) - 1
    )
    (config.workspace_root / "large.png").write_bytes(PNG_BYTES)
    (config.workspace_root / "empty.png").write_bytes(b"")
    (config.workspace_root / "fake.png").write_bytes(b"not an image")

    with pytest.raises(ValueError, match="max is"):
        await image_ops.read_image_execute(
            config, store, "large.png", session_id
        )

    config = replace(config, max_view_image_bytes=1024)
    with pytest.raises(ValueError, match="empty"):
        await image_ops.read_image_execute(
            config, store, "empty.png", session_id
        )
    with pytest.raises(ValueError, match="Unsupported image format"):
        await image_ops.read_image_execute(
            config, store, "fake.png", session_id
        )


@pytest.mark.asyncio
async def test_executor_view_image_returns_native_mcp_content(tmp_path: Path):
    config, store, session_id = _executor(tmp_path)
    (config.workspace_root / "pixel.png").write_bytes(PNG_BYTES)

    result = await image_ops.view_image_execute(
        config, store, "pixel.png", session_id
    )

    assert isinstance(result.content[0], ImageContent)
    assert base64.b64decode(result.content[0].data) == PNG_BYTES
    assert isinstance(result.content[1], TextContent)
    assert result.structuredContent == {
        "session_id": session_id,
        "path": "pixel.png",
        "mime_type": "image/png",
        "bytes": len(PNG_BYTES),
    }


@pytest.mark.asyncio
async def test_view_image_tool_returns_native_mcp_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    (tmp_path / "pixel.png").write_bytes(PNG_BYTES)
    mcp = build_mcp(
        runtime=build_paired_control_harness(get_settings()).control
    )
    session_id = mcp_structured(
        await mcp.call_tool("session_start", {"workdir": "."})
    )["session_id"]
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
    assert response.structuredContent == {
        "session_id": session_id,
        "path": "pixel.png",
        "mime_type": "image/png",
        "bytes": len(PNG_BYTES),
    }
