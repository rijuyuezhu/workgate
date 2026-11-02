"""MCP App image preview metadata, strict link validation and lifetime."""

import hashlib

import pytest
from mcp.server.mcpserver.exceptions import ResourceError, ToolError

from workgate.config.control import resolve_control_config
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.download_snapshot import (
    DownloadSnapshot,
    new_staging_path,
    open_private_staging,
)
from workgate.control.downloads import (
    _register_snapshot,
    _revoke_file_link_owned,
)
from workgate.control.mcp.app import build_mcp
from workgate.persistence import FileStateStore, use_state_store


@pytest.fixture
def image_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_MODE", "mcp")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    monkeypatch.setenv("WORKGATE_UI_ENABLED", "true")
    monkeypatch.setenv("WORKGATE_FILE_DOWNLOAD_ENABLED", "true")
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("WORKGATE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKGATE_BASE_URL", "https://files.example.test")
    clear_settings_cache()
    return get_settings()


def _image_link(settings, tmp_path, *, suffix=".png", inline=True):
    file_name = f"picture{suffix}"
    content = b"\x89PNG\r\n\x1a\nexample" if suffix == ".png" else b"image-data"
    staged = new_staging_path(data_dir=settings.data_dir)
    with open_private_staging(staged, data_dir=settings.data_dir) as stream:
        stream.write(content)
    return _register_snapshot(
        DownloadSnapshot(
            staging_path=staged,
            source_name=file_name,
            display_path=str(tmp_path / file_name),
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        ),
        ttl_s=60,
        filename=None,
        max_downloads=0,
        inline=inline,
        session_id=None,
        settings=__import__(
            "workgate.config.control", fromlist=["resolve_control_config"]
        ).resolve_control_config(settings),
    )


@pytest.mark.asyncio
async def test_image_preview_mcp_app_metadata_and_asset(image_settings):
    mcp = build_mcp()
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    show = tools["image_show"]
    assert show.meta is not None
    assert show.meta["ui"]["resourceUri"] == show.meta["openai/outputTemplate"]
    assert show.meta["ui"]["resourceUri"].startswith(
        "ui://workgate/image-preview-"
    )
    assert (
        show.annotations is not None and show.annotations.read_only_hint is True
    )
    assert "url" in show.input_schema["required"]
    assert show.meta["securitySchemes"] == [{"type": "noauth"}]

    resources = {str(item.uri): item for item in await mcp.list_resources()}
    resource = resources["ui://workgate/image-preview.html"]
    assert resource.mime_type == "text/html;profile=mcp-app"
    assert resource.meta is not None
    assert resource.meta["ui"]["csp"] == {
        "connectDomains": [],
        "resourceDomains": ["https://files.example.test"],
    }
    assert resource.meta["ui"]["domain"] == "https://files.example.test"
    assert "ui://workgate/image-preview-{digest}.html" in {
        item.uri_template for item in await mcp.list_resource_templates()
    }
    assert "Workgate Image Preview" in str(
        await mcp.read_resource(show.meta["ui"]["resourceUri"])
    )
    with pytest.raises(ResourceError, match="cache key"):
        await mcp.read_resource("ui://workgate/image-preview-invalid.html")


@pytest.mark.asyncio
async def test_image_preview_accepts_only_live_inline_image_links(
    image_settings, tmp_path, monkeypatch
):
    config = resolve_control_config(image_settings)
    with use_state_store(FileStateStore(lambda: config.state_dir)):
        png = _image_link(image_settings, tmp_path)
        jpg = _image_link(image_settings, tmp_path, suffix=".jpg")
        attachment = _image_link(image_settings, tmp_path, inline=False)
        nonimage = _image_link(image_settings, tmp_path, suffix=".txt")
        mcp = build_mcp()

        for link, mime in ((png, "image/png"), (jpg, "image/jpeg")):
            result = await mcp.call_tool("image_show", {"url": link.url})
            assert result.structured_content == {
                "url": link.url,
                "media_type": mime,
            }
            assert not result.is_error

        wrong_urls = [
            png.url.replace("files.example.test", "evil.example.test"),
            png.url.replace(
                "files.example.test", "files.example.test.evil.test"
            ),
            png.url.replace("https://", "http://"),
            png.url + "?next=https://evil.test",
            png.url + "#fragment",
            png.url.replace("/download/", "/download/../download/"),
            png.url.replace("/download/", "/download/%2e%2e/"),
            png.url.replace("/download/", "/download/extra/"),
            png.url + "/",
            "https://files.example.test/download/" + "a" * 43,
            attachment.url,
            nonimage.url,
        ]
        for url in wrong_urls:
            with pytest.raises(ToolError):
                await mcp.call_tool("image_show", {"url": url})

        assert _revoke_file_link_owned(png.link_id, settings=config).revoked
        with pytest.raises(ToolError, match="unavailable or expired"):
            await mcp.call_tool("image_show", {"url": png.url})

        # The preview tool checks validity without consuming the bearer URL.
        from workgate.control.download_store import (
            ClaimedDownload,
            release_claim,
        )
        from workgate.control.downloads import claim_download

        checked = claim_download(jpg.token, consume=False, settings=config)
        assert isinstance(checked, ClaimedDownload)
        assert checked.link["downloads"] == 0
        release_claim(checked)

        from workgate.control import downloads

        monkeypatch.setattr(
            downloads, "_now_s", lambda: float(jpg.created_at) + 61
        )
        with pytest.raises(ToolError, match="unavailable or expired"):
            await mcp.call_tool("image_show", {"url": jpg.url})


@pytest.mark.asyncio
async def test_image_preview_disabled_with_downloads_or_ui(
    tmp_path, monkeypatch
):
    for name in ("WORKGATE_FILE_DOWNLOAD_ENABLED", "WORKGATE_UI_ENABLED"):
        monkeypatch.setenv("WORKGATE_MODE", "mcp")
        monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
        monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv(name, "false")
        clear_settings_cache()
        assert "image_show" not in {
            tool.name for tool in await build_mcp().list_tools()
        }
        monkeypatch.delenv(name)


@pytest.mark.asyncio
async def test_image_preview_disabled_in_stdio(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_MODE", "stdio")
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / "state"))
    clear_settings_cache()
    assert "image_show" not in {
        tool.name for tool in await build_mcp().list_tools()
    }
