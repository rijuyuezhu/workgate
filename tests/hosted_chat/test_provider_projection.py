"""Pinned upstream message/media projection tests and native bridge contracts."""

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from workgate.agent_bridge.mcp import normalize_tool_result
from workgate.hosted_chat.mcp_content import (
    format_tool_content,
    mcp_result_envelope,
)
from workgate.hosted_chat.mcp_projection import (
    HARD_MAX_TOOL_RESULT_CHARS,
    _Accumulator,
    _compact_segment,
    _count,
    mcp_tool_history_message,
    openai_wire_messages,
    project_artifact_messages,
)
from workgate.hosted_chat.provider_multimodal import (
    inline_media,
    native_content_parts,
)
from workgate.hosted_chat.result_projection import truncate_approval_result

_REF = Path(__file__).parents[1] / "fixtures" / "hosted_chat"
_VECTORS = json.loads(
    (_REF / "mcp_content_vectors.json").read_text(encoding="utf-8")
)
_IMAGE = {
    "type": "image_url",
    "image_url": {"url": "data:image/png;base64,AA=="},
}
_FILE = {
    "type": "file",
    "file": {
        "file_data": "data:application/pdf;base64,YQ==",
        "filename": "paper.pdf",
    },
}


@pytest.mark.parametrize("case", _VECTORS["vectors"], ids=lambda c: c["name"])
def test_differential_real_librechat_mcp_parser(case):
    assert (
        _VECTORS["sha256"]
        == "85170923b508332f4f9a647eeafbb7ac6d79d6d3fbb606d6f4d1838d7623d584"
    )
    result = format_tool_content(case["result"], case["provider"])
    if case["name"] == "image_remote":
        assert result == (
            "[Omitted invalid, remote or oversized MCP image]",
            None,
        )
    elif case["name"] == "rich_ui":
        assert result == (
            "A\n\nMCP UI resource (not included in model context)\nResource URI: ui://screen\nResource MIME Type: text/html",
            None,
        )
    else:
        assert list(result) == case["expected"]


def test_source_utf16_truncation_oracle():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for source-oracle parity")
    contents = [
        "short",
        "a" * 1000,
        "😀" * 600,
        ("header\n" + "a" * 40 + "\n") * 80,
    ]
    cases = [
        [content, budget]
        for content in contents
        for budget in [0, 1, 2, 5, 65, 110, 250, 500, 5000]
    ]
    program = "const p=require(process.argv[1]); const fs=require('fs'); const cases=JSON.parse(fs.readFileSync(0,'utf8')); process.stdout.write(JSON.stringify(cases.map(([s,n])=>p.truncateToolResultContent(s,n))));"
    result = subprocess.run(
        [
            node,
            "-e",
            program,
            str((_REF / "approval_runtime_reference.cjs").resolve()),
        ],
        input=json.dumps(cases),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert [truncate_approval_result(*case) for case in cases] == json.loads(
        result.stdout
    )


def test_native_media_projection():
    blocks = [{"type": "text", "text": "look"}, _IMAGE, _FILE]
    native = native_content_parts(blocks, "anthropic")
    assert native[0] == {"type": "text", "text": "look"}
    assert native[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AA=="},
    }
    assert native[2]["type"] == "document"
    google = native_content_parts(blocks, "google")
    assert google[0] == {"text": "look"}
    assert google[1]["inlineData"]["mimeType"] == "image/png"
    assert google[2]["inlineData"]["mimeType"] == "application/pdf"
    assert native_content_parts("Plain message", "anthropic") == [
        {"type": "text", "text": "Plain message"}
    ]
    assert native_content_parts("", "google") == []
    assert native_content_parts(None, "google") == []
    assert (
        native_content_parts(
            [
                "text",
                None,
                {},
                {"type": "audio"},
                {"type": "text", "text": None},
            ],
            "google",
        )
        == []
    )
    with pytest.raises(ValueError, match="Unsupported native"):
        native_content_parts("test", "other")


@pytest.mark.parametrize(
    "part",
    [
        {
            "type": "image_url",
            "image_url": {"url": "https://untrusted.invalid/x.png"},
        },
        {"type": "image_url", "image_url": {"url": 123}},
        {"type": "image_url"},
        {"type": "file"},
        {"type": "image_url", "image_url": "not-an-object"},
        {"type": "file", "file": "not-an-object"},
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + "x" * (8 * 1024 * 1024)
            },
        },
        {
            "type": "image_url",
            "image_url": {"url": "data:image/svg+xml;base64,YQ=="},
        },
        {
            "type": "file",
            "file": {"file_data": "data:application/pdf;base64,!!"},
        },
        {
            "type": "file",
            "file": {"file_data": "data:application/pdf;base64,YQ==="},
        },
        {"type": "file", "file": {"file_id": "external-file"}},
        {
            "type": "image_url",
            "image_url": {"url": "data:application/pdf;base64,YQ=="},
        },
        {"type": "file", "file": {"file_data": "data:image/png;base64,YQ=="}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,"}},
    ],
)
def test_untrusted_media_rejected_on_each_wire(part):
    assert native_content_parts([part], "anthropic") == []
    assert native_content_parts([part], "google") == []
    with pytest.raises(ValueError, match="canonical inline"):
        openai_wire_messages([{"role": "user", "content": [part]}])


def _anchor(*ids):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": cid,
                "type": "function",
                "function": {"name": "call_agent_mcp_tool", "arguments": "{}"},
                "provider_data": {"opaque": "secret"},
            }
            for cid in ids
        ],
    }


def test_native_mcp_result_replay_preserves_raw_and_order():
    raw = json.dumps(
        normalize_tool_result(
            {
                "content": [
                    {"type": "image", "mimeType": "image/png", "data": "AA=="}
                ],
                "isError": True,
                "structuredContent": {"answer": 42},
            }
        )
    )
    result = mcp_tool_history_message(
        "first", "call_agent_mcp_tool", raw, "openai"
    )
    assert result["raw_result"] == raw and result["is_error"] is True
    assert result["content"] == 'Structured content: {"answer":42}'
    assert result["artifact"]["content"] == [_IMAGE]
    plain = mcp_tool_history_message("second", "read_file", raw, "openai")
    assert plain["content"] == raw and "artifact" not in plain
    for bad in ("not json", "{}"):
        assert (
            mcp_tool_history_message("x", "call_agent_mcp_tool", bad, "openai")[
                "content"
            ]
            == bad
        )
    original = [
        _anchor("first", "second"),
        result,
        plain,
        {"role": "assistant", "content": "Rendered"},
        {"role": "user", "content": "Next"},
    ]
    before = copy.deepcopy(original)
    openai = project_artifact_messages(original, "openai")
    assert [r["role"] for r in openai] == [
        "assistant",
        "tool",
        "tool",
        "user",
        "assistant",
        "user",
    ]
    assert openai[3]["content"][-1] == _IMAGE
    assert openai[1]["content"].startswith("Tool response is included")
    anthropic = project_artifact_messages(original, "anthropic")
    assert anthropic[1]["content"][-1] == _IMAGE
    wire = openai_wire_messages(original)
    assert wire[3]["content"][-1] == _IMAGE
    assert "provider_data" not in wire[0]["tool_calls"][0]
    assert "raw_result" not in wire[1] and "artifact" not in wire[1]
    assert original == before
    assert project_artifact_messages(original[:2], "openai") == original[:2]


def test_projection_bounds_and_atomic_media():
    assert _count("😀") == 2
    assert (
        _count([{"type": "text", "text": "A"}, {"type": "text", "text": "B"}])
        == 3
    )
    assert _compact_segment([{"type": "text", "text": "text"}], 100) == [
        {"type": "text", "text": "text"}
    ]
    assert _compact_segment([{"type": "text", "text": "long"}], 2) == "lo"
    assert _compact_segment([_IMAGE], 1) == "["
    assert isinstance(_compact_segment([_IMAGE], 40), list)
    with pytest.raises(ValueError, match="Unsupported"):
        _compact_segment([{"type": "unexpected"}], 20)
    accumulator = _Accumulator(4)
    accumulator.append("first")
    accumulator.append("second")
    assert accumulator.remaining >= 0 and accumulator.has_content
    accumulator.append([_IMAGE, _IMAGE])
    assert accumulator.remaining >= 0
    raw = "😀" * 210000
    original = [{"role": "tool", "tool_call_id": "large", "content": raw}]
    wire = openai_wire_messages(original)
    assert _count(wire[0]["content"]) <= HARD_MAX_TOOL_RESULT_CHARS
    assert "truncated" in wire[0]["content"]
    assert original[0]["content"] == raw
    result = mcp_tool_history_message(
        "id",
        "call_agent_mcp_tool",
        json.dumps(
            {
                "content": [
                    {"type": "image", "mimeType": "image/png", "data": "AA=="}
                ]
            }
        ),
        "openai",
    )
    assert (
        project_artifact_messages(
            [_anchor("id"), result], "openai", max_chars=0
        )[1]["content"]
        == "[Omitted tool content]"
    )
    assert (
        project_artifact_messages(
            [_anchor("id"), result], "anthropic", max_chars=0
        )[1]["content"]
        == "[Omitted tool content]"
    )


def test_openai_payload_sanitizes_local_fields():
    before = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look", "private": "x"},
                _IMAGE,
                _FILE,
            ],
            "source": "steer",
            "files": [{"id": "local"}],
        }
    ]
    wire = openai_wire_messages(before)
    assert wire == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "look"}, _IMAGE, _FILE],
        }
    ]
    for part in (None, {"type": "unrecognized"}):
        with pytest.raises(ValueError, match="provider content part"):
            openai_wire_messages([{"role": "user", "content": [part]}])
    assert inline_media("data:image/png;base64,AA==", kind="image_url") == (
        "image/png",
        "AA==",
    )


def test_invalid_mcp_resource_and_image_do_not_destroy_raw_result():
    source = {
        "content": [
            {"type": "image", "data": "!!", "mimeType": "image/png"},
            {"type": "image", "data": "AA==", "mime_type": "image/png"},
            {
                "type": "resource",
                "resource": {"uri": "file:///a", "blob": "!!"},
            },
            {"type": "resource", "resource": {"uri": "file:///b", "blob": 3}},
            {
                "type": "resource",
                "resource": {
                    "uri": "file:///c",
                    "mime_type": "image/png",
                    "blob": "!!",
                },
            },
        ]
    }
    raw = json.dumps(source)
    projected = mcp_tool_history_message(
        "id", "call_agent_mcp_tool", raw, "openai"
    )
    assert projected["raw_result"] == raw
    assert projected["artifact"]["content"] == [_IMAGE]
    assert "invalid base64" in projected["content"]
    assert "oversized or invalid blob" in projected["content"]
    assert "Omitted invalid" in projected["content"]
    assert mcp_result_envelope({"content": [{"type": 3}]}) is None
    assert format_tool_content({"content": "bad"}) == ("(No response)", None)
    assert format_tool_content(
        {
            "content": [
                None,
                {"type": "text", "text": 3},
                {"type": "resource", "resource": "bad"},
            ]
        }
    ) == ("(No response)", None)


def test_sdk_mcp_content_and_structured_only_result():
    from mcp.types import CallToolResult, ImageContent

    result = normalize_tool_result(
        CallToolResult(
            content=[
                ImageContent(type="image", data="AA==", mime_type="image/png")
            ],
            structured_content={"answer": 42},
            is_error=True,
        )
    )
    raw = json.dumps(result)
    message = mcp_tool_history_message(
        "image", "call_agent_mcp_tool", raw, "anthropic"
    )
    assert message["raw_result"] == raw
    assert message["artifact"]["content"] == [_IMAGE]
    assert message["is_error"] is True
    assert message["content"] == 'Structured content: {"answer":42}'
    raw = json.dumps(
        normalize_tool_result(
            CallToolResult(content=[], structured_content={"ok": True})
        )
    )
    message = mcp_tool_history_message(
        "structured", "call_agent_mcp_tool", raw, "google"
    )
    assert message["content"] == 'Structured content: {"ok":true}'
    assert "artifact" not in message


def test_duplicate_result_is_not_projected_as_complete_batch():
    raw = json.dumps(
        {
            "content": [
                {"type": "image", "mimeType": "image/png", "data": "AA=="}
            ]
        }
    )
    result = mcp_tool_history_message(
        "one", "call_agent_mcp_tool", raw, "openai"
    )
    original = [_anchor("one"), result, result]
    assert project_artifact_messages(original, "openai") == original
