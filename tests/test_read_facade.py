import pytest

from tests.helpers import mcp_structured
from workgate.config.settings import clear_settings_cache
from workgate.control.mcp.app import build_mcp
from workgate.tool_session.selectors import parse_read_target


def test_parse_read_target_rejects_missing_plus_count():
    with pytest.raises(ValueError, match=r"invalid read line selector: 10\+"):
        parse_read_target("demo.py:10+")


@pytest.mark.asyncio
async def test_read_facade_reads_line_selector_with_numbered_content(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    (tmp_path / "demo.py").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    session = mcp_structured(
        await build_mcp().call_tool("session_start", {"workdir": "."})
    )
    response = await build_mcp().call_tool(
        "read", {"session_id": session["session_id"], "path": "demo.py:2-3"}
    )
    result = mcp_structured(response)

    assert result["kind"] == "file"
    snapshot_id = result["file"]["snapshot_id"]
    assert result["content"] == f"[demo.py#{snapshot_id}]\n2:beta\n3:gamma"
    assert "content" not in result["file"]
    assert "numbered_content" not in result["file"]
    assert "lines" not in result["file"]
    assert result["file"]["start_line"] == 2
    assert result["file"]["seen_ranges"] == [{"start": 2, "end": 3}]


@pytest.mark.asyncio
async def test_read_facade_reads_multi_range_selector_with_grounding(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    (tmp_path / "demo.py").write_text(
        "alpha\nbeta\ngamma\ndelta\nepsilon\n", encoding="utf-8"
    )

    session = mcp_structured(
        await build_mcp().call_tool("session_start", {"workdir": "."})
    )
    response = await build_mcp().call_tool(
        "read",
        {"session_id": session["session_id"], "path": "demo.py:2-3,5-5"},
    )
    result = mcp_structured(response)

    assert result["kind"] == "file"
    snapshot_id = result["file"]["snapshot_id"]
    assert result["content"] == (
        f"[demo.py#{snapshot_id}]\n2:beta\n3:gamma\n5:epsilon"
    )
    assert result["file"]["start_line"] == 2
    assert result["file"]["end_line"] == 5
    assert result["file"]["line_count"] == 3
    assert result["file"]["seen_ranges"] == [
        {"start": 2, "end": 3},
        {"start": 5, "end": 5},
    ]


@pytest.mark.asyncio
async def test_read_facade_raw_multi_range_selector_returns_unnumbered_content(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    (tmp_path / "demo.py").write_text(
        "alpha\nbeta\ngamma\ndelta\n", encoding="utf-8"
    )

    session = mcp_structured(
        await build_mcp().call_tool("session_start", {"workdir": "."})
    )
    response = await build_mcp().call_tool(
        "read",
        {"session_id": session["session_id"], "path": "demo.py:2-2,4-4:raw"},
    )
    result = mcp_structured(response)

    assert result["kind"] == "file"
    assert result["raw"] is True
    assert result["content"] == "beta\ndelta"
    assert result["file"]["seen_ranges"] == [
        {"start": 2, "end": 2},
        {"start": 4, "end": 4},
    ]


@pytest.mark.asyncio
async def test_read_facade_raw_selector_returns_unnumbered_content(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    (tmp_path / "demo.py").write_bytes(b"alpha\nbeta\n")

    session = mcp_structured(
        await build_mcp().call_tool("session_start", {"workdir": "."})
    )
    response = await build_mcp().call_tool(
        "read", {"session_id": session["session_id"], "path": "demo.py:raw"}
    )
    result = mcp_structured(response)

    assert result["kind"] == "file"
    assert result["raw"] is True
    assert result["content"] == "alpha\nbeta\n"
    assert "content" not in result["file"]
    assert "numbered_content" not in result["file"]
    assert "lines" not in result["file"]
    assert result["file"]["seen_ranges"] == [{"start": 1, "end": 2}]


@pytest.mark.asyncio
async def test_read_facade_lists_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "demo.py").write_text("", encoding="utf-8")

    session = mcp_structured(
        await build_mcp().call_tool("session_start", {"workdir": "."})
    )
    response = await build_mcp().call_tool(
        "read", {"session_id": session["session_id"], "path": "pkg"}
    )
    result = mcp_structured(response)

    assert result["kind"] == "directory"
    assert "file\tpkg/demo.py" in result["content"]
    assert result["directory"]["count"] == 1
