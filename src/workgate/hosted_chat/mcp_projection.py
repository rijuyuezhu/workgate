"""Provider-visible SDK artifact projection for JSON-native MCP image results.

Source: agents 4.0.1 messages/core.ts projectArtifactPayload and
projectAnthropicArtifactContent. The original raw native JSON remains in owned
checkpoint writes; parsed ToolMessage artifacts remain in graph history. Only
this temporary projection is serialized to a model provider. Full provenance
and non-MCP artifact families belong to their separate Graph/Artifacts ports.
"""

import copy
import json
import re
from typing import Any

from .mcp_content import format_tool_content, mcp_result_envelope
from .provider_multimodal import inline_media
from .result_projection import truncate_approval_result

HARD_MAX_TOOL_RESULT_CHARS = 400000
_ARTIFACT_PLACEHOLDER = (
    "Tool response is included in the next message as a Human message"
)


def _length(value: str) -> int:
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def _compact_json(value) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = text.encode("utf-16-le", errors="surrogatepass").decode(
        "utf-16-le", errors="surrogatepass"
    )
    return re.sub(
        r"[\ud800-\udfff]",
        lambda match: "\\u" + format(ord(match[0]), "04x"),
        text,
    )


def _count(value) -> int:
    if isinstance(value, str):
        return _length(value)
    if isinstance(value, list) and all(
        isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        for block in value
    ):
        return sum(_length(block["text"]) for block in value) + max(
            0, len(value) - 1
        )
    return _length(_compact_json(value))


def _compact_segment(value, budget):
    """The source compactToolContent branches reached by parser image artifacts."""
    if isinstance(value, str):
        return truncate_approval_result(value, budget)
    if len(value) != 1 or value[0].get("type") not in {"image_url", "text"}:
        raise ValueError("Unsupported non-MCP artifact content")
    if _count(value) <= budget:
        return copy.deepcopy(value)
    block = value[0]
    if block["type"] == "text":
        return truncate_approval_result(block["text"], budget)
    # A single image that cannot fit its own segment cannot fit half of that
    # budget either. Mirror the SDK atomic-omission and structural allowance.
    preview = truncate_approval_result(
        "[omitted 1 oversized atomic tool-content block: image_url]",
        max(0, budget - 32),
    )
    if preview:
        return [{"type": "text", "text": preview}]
    return "[" if budget == 1 else []


class _Accumulator:
    def __init__(self, max_chars):
        self.blocks = []
        self.has_content = False
        self.remaining = max(0, min(HARD_MAX_TOOL_RESULT_CHARS, int(max_chars)))

    def append(self, value):
        if isinstance(value, list):
            for block in value:
                self._segment([block])
                if self.remaining <= int(self.has_content):
                    break
        else:
            self._segment(value)

    def _segment(self, value):
        separator = int(self.has_content)
        available = self.remaining - separator
        if _count(value) <= 0 or available <= 0:
            return
        compacted = _compact_segment(value, available)
        appended = min(available, _count(compacted))
        if appended <= 0:
            return
        self.blocks.extend(
            [{"type": "text", "text": compacted}]
            if isinstance(compacted, str)
            else compacted
        )
        self.has_content = True
        self.remaining -= separator + appended


def project_artifact_messages(
    messages: list[dict], provider: str, max_chars=HARD_MAX_TOOL_RESULT_CHARS
) -> list[dict]:
    """Project all completed tool batches at their original history positions.

    The SDK projects immediately after each ToolMessage in a live graph. A
    reloaded conversation ends in the *next* HumanMessage, so testing only
    messages[-1] would silently lose all earlier images on SQLite replay.
    The stored DTO is never mutated.
    """
    projected = []
    changed = False
    index = 0
    while index < len(messages):
        message = messages[index]
        projected.append(message)
        index += 1
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        call_ids = {call.get("id") for call in message["tool_calls"]}
        batch = []
        while (
            index < len(messages)
            and messages[index].get("role") == "tool"
            and messages[index].get("tool_call_id") in call_ids
        ):
            batch.append(messages[index])
            index += 1
        with_artifacts = [
            msg
            for msg in batch
            if isinstance(msg.get("artifact"), dict)
            and msg["artifact"].get("content")
        ]
        if (
            not with_artifacts
            or len(batch) != len(call_ids)
            or {item.get("tool_call_id") for item in batch} != call_ids
        ):
            projected.extend(batch)
            continue
        changed = True
        aggregate = _Accumulator(max_chars)
        for item in batch:
            artifact = item.get("artifact")
            if not isinstance(artifact, dict) or not artifact.get("content"):
                projected.append(item)
                continue
            clone = {**item, "artifact": {**artifact, "content": []}}
            if provider == "anthropic":
                current = _Accumulator(max_chars)
                base = item.get("content", "")
                # Anthropic rejects empty text blocks in tool_result content.
                if base:
                    current.append(base)
                current.append(artifact["content"])
                clone["content"] = current.blocks or "[Omitted tool content]"
            else:
                before = aggregate.remaining
                aggregate.append(item.get("content", ""))
                aggregate.append(artifact["content"])
                clone["content"] = (
                    _ARTIFACT_PLACEHOLDER
                    if aggregate.remaining < before
                    else "[Omitted tool content]"
                )
            projected.append(clone)
        if provider != "anthropic" and aggregate.has_content:
            projected.append({"role": "user", "content": aggregate.blocks})
    return projected if changed else messages


def mcp_tool_history_message(
    call_id: str, tool_name: str, raw: str, provider: str
) -> dict:
    """Project only the known native Agent Bridge dispatch; retain exact raw output."""
    message: dict[str, Any] = {
        "role": "tool",
        "tool_call_id": call_id,
        "content": raw,
    }
    if tool_name != "call_agent_mcp_tool":
        return message
    message["raw_result"] = raw
    try:
        envelope = mcp_result_envelope(json.loads(raw))
    except ValueError, TypeError:
        return message
    if envelope is None:
        return message
    return _project_mcp_envelope(message, envelope, provider)


def native_tool_history_message(
    call_id: str, envelope: dict, provider: str
) -> dict:
    """Project a trusted native dispatcher envelope, not arbitrary tool JSON."""
    if mcp_result_envelope(envelope) is None:
        raise ValueError("Invalid native MCP result envelope")
    message = {"role": "tool", "tool_call_id": call_id}
    projected = _project_mcp_envelope(message, envelope, provider)
    projected["content"] = truncate_approval_result(
        projected["content"], HARD_MAX_TOOL_RESULT_CHARS
    )
    return projected


def _project_mcp_envelope(message: dict, envelope: dict, provider: str) -> dict:
    text, artifact = format_tool_content(envelope, provider)
    # Structured-only MCP responses must not disappear.
    structured = envelope.get(
        "structured_content", envelope.get("structuredContent")
    )
    if structured is not None:
        if "raw_result" not in message and text:
            # Native creation adapters provide a JSON text fallback of the
            # same structured receipt. Include it only once in model history.
            try:
                if json.loads(text) == structured:
                    text = ""
            except ValueError:
                pass
        text = (
            (text + "\n\n" if text and text != "(No response)" else "")
            + "Structured content: "
            + _compact_json(structured)
        )
    message["content"] = text
    message["is_error"] = bool(
        envelope.get("isError", envelope.get("is_error", False))
    )
    if artifact is not None:
        message["artifact"] = artifact
    return message


def openai_wire_messages(messages: list[dict]) -> list[dict]:
    """Provider serialization drops checkpoint-local artifact/id/provenance."""
    result = []
    for message in project_artifact_messages(messages, "openai"):
        item = {
            key: copy.deepcopy(message[key])
            for key in ("role", "content", "name", "tool_call_id")
            if key in message
        }
        if isinstance(item.get("content"), list):
            parts = []
            for part in item["content"]:
                if not isinstance(part, dict):
                    raise ValueError("Invalid provider content part")
                kind = part.get("type")
                if kind == "text" and isinstance(part.get("text"), str):
                    parts.append({"type": "text", "text": part["text"]})
                elif kind in {"image_url", "file"}:
                    fields = part.get(
                        "image_url" if kind == "image_url" else "file"
                    )
                    url = (
                        fields.get(
                            "url" if kind == "image_url" else "file_data"
                        )
                        if isinstance(fields, dict)
                        else None
                    )
                    if (
                        not isinstance(fields, dict)
                        or not isinstance(url, str)
                        or inline_media(url, kind=kind) is None
                    ):
                        raise ValueError(
                            "Provider media requires canonical inline data"
                        )
                    if kind == "image_url":
                        image = {"url": url}
                        if fields.get("detail") in ("auto", "low", "high"):
                            image["detail"] = fields["detail"]
                        parts.append({"type": kind, "image_url": image})
                    else:
                        file = {"file_data": url}
                        if isinstance(fields.get("filename"), str):
                            file["filename"] = fields["filename"]
                        parts.append({"type": kind, "file": file})
                else:
                    raise ValueError("Unsupported provider content part")
            item["content"] = parts
        content = item.get("content")
        if message.get("role") == "tool" and isinstance(content, str):
            # Source @librechat/agents utils/truncation.ts: bounded model
            # ingestion. The persisted raw result remains completely intact.
            item["content"] = truncate_approval_result(
                content, HARD_MAX_TOOL_RESULT_CHARS
            )
        if message.get("tool_calls"):
            item["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        key: call["function"][key]
                        for key in ("name", "arguments")
                    },
                }
                for call in message["tool_calls"]
            ]
        result.append(item)
    return result
