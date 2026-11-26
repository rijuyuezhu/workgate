"""Strict Anthropic/Gemini wire projections for the shared canonical message DTO.

Sources: LibreChat @librechat/agents 4.0.1 source and its pinned
@langchain/anthropic/@langchain/google-genai adapters. Do not serialize
checkpoint-local raw_result, UI metadata or unsupported content blocks.
"""

import json
import secrets
from collections.abc import AsyncIterator

from .provider_common import (
    MAX_TOOL_ARGUMENTS,
    MAX_TOOL_CALLS,
    ModelFinish,
    ModelThought,
    ModelToolCall,
    ModelUsage,
    ProviderConfig,
    _anthropic_omits_sampling_parameters,
    _data_frames,
    _usage_frame,
    complete_tool_batch,
    provider_http_client,
    provider_scope,
)
from .provider_multimodal import native_content_parts
from .result_projection import truncate_approval_result


def _content_blocks(blocks: list[dict], provider: str) -> list[dict]:
    """Only model-supported types are admitted to the vendor wire request."""
    result = []
    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            result.append(
                {"type": "text", "text": block["text"]}
                if provider == "anthropic"
                else {"text": block["text"]}
            )
        elif kind in {"image_url", "file"}:
            converted = native_content_parts([block], provider)
            if not converted:
                raise ValueError(
                    "Provider media requires canonical inline data"
                )
            result.extend(converted)
        else:
            raise ValueError(f"Unsupported {provider} content block: {kind}")
    return result


def _reasoning_blocks(message, provider, scope):
    if provider != "anthropic" or scope is None:
        return []
    blocks = []
    for part in message.get("reasoning") or []:
        if part.get("scope") != scope:
            continue
        if part.get("type") == "thinking" and part.get("signature"):
            blocks.append(
                {
                    "type": "thinking",
                    "thinking": part.get("thinking", ""),
                    "signature": part["signature"],
                }
            )
        elif part.get("type") == "redacted_thinking" and part.get("data"):
            blocks.append({"type": "redacted_thinking", "data": part["data"]})
    return blocks


def native_messages(
    messages: list[dict], provider: str, *, scope: str | None = None
) -> tuple[list[dict], str]:
    """Serialize ordered assistant/tool/steer messages without lossy str()."""
    from .mcp_projection import project_artifact_messages

    messages = project_artifact_messages(messages, provider)
    result: list[dict] = []
    systems: list[str] = []
    names: dict[str, str] = {}
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            if isinstance(content, str):
                systems.append(content)
            elif isinstance(content, list):
                systems.extend(
                    part.get("text", "")
                    for part in content
                    if part.get("type") == "text"
                )
            else:
                raise ValueError("Invalid system instruction")
            continue
        if role not in {"user", "assistant", "tool"}:
            raise ValueError("Unsupported message role")
        parts = []
        if role == "tool":
            identity = message.get("tool_call_id")
            if not isinstance(identity, str) or not identity:
                raise ValueError("Tool result missing call ID")
            if isinstance(content, str):
                rendered = truncate_approval_result(content, 400_000)
            elif isinstance(content, list):
                rendered = _content_blocks(content, provider)
            else:
                raise ValueError("Invalid tool content")
            if provider == "anthropic":
                parts = [
                    {
                        "type": "tool_result",
                        "tool_use_id": identity,
                        "content": rendered,
                        **(
                            {"is_error": True}
                            if message.get("is_error")
                            else {}
                        ),
                    }
                ]
            else:
                if identity not in names:
                    raise ValueError(
                        "Gemini function response has no matching function call"
                    )
                response = {"result": rendered}
                if message.get("is_error"):
                    response["is_error"] = True
                parts = [
                    {
                        "functionResponse": {
                            "name": names[identity],
                            "response": response,
                        }
                    }
                ]
            native_role = "user"
        else:
            native_role = (
                "model"
                if provider == "google" and role == "assistant"
                else role
            )
            if isinstance(content, str):
                if content:
                    parts.append(
                        {"type": "text", "text": content}
                        if provider == "anthropic"
                        else {"text": content}
                    )
            elif isinstance(content, list):
                parts.extend(_content_blocks(content, provider))
            elif content is not None:
                raise ValueError("Invalid message content")
            if role == "assistant":
                parts = [*_reasoning_blocks(message, provider, scope), *parts]
            for call in message.get("tool_calls") or []:
                function = call["function"]
                identity = call["id"]
                names[identity] = function["name"]
                arguments = function["arguments"]
                arguments = (
                    json.loads(arguments)
                    if isinstance(arguments, str)
                    else arguments
                )
                if not isinstance(arguments, dict):
                    raise ValueError("Tool call arguments must be an object")
                if provider == "anthropic":
                    parts.append(
                        {
                            "type": "tool_use",
                            "id": identity,
                            "name": function["name"],
                            "input": arguments,
                        }
                    )
                else:
                    item = {
                        "functionCall": {
                            "name": function["name"],
                            "args": arguments,
                        }
                    }
                    signature = (call.get("provider_data") or {}).get(
                        "thoughtSignature"
                    )
                    if (
                        signature
                        and scope is not None
                        and (call.get("provider_data") or {}).get("scope")
                        == scope
                    ):
                        item["thoughtSignature"] = signature
                    parts.append(item)
        key = "content" if provider == "anthropic" else "parts"
        if not parts:
            continue
        # Original Anthropic adapter uses strings for ordinary text-only turns.
        if (
            provider == "anthropic"
            and role in {"user", "assistant"}
            and isinstance(content, str)
            and content
            and not message.get("tool_calls")
            and not _reasoning_blocks(message, provider, scope)
        ):
            parts = content
        # Anthropic permits multiple adjacent tool_result blocks in one user
        # message. Gemini requires functionResponse parts in its own user turn.
        if (
            provider == "anthropic"
            and role == "tool"
            and result
            and result[-1]["role"] == "user"
            and isinstance(result[-1][key], list)
            and all(
                part.get("type") == "tool_result" for part in result[-1][key]
            )
        ):
            result[-1][key].extend(parts)
        elif result and result[-1]["role"] == native_role:
            existing = result[-1][key]
            if isinstance(existing, str):
                existing = (
                    [{"type": "text", "text": existing}]
                    if provider == "anthropic"
                    else [{"text": existing}]
                )
            if isinstance(parts, str):
                parts = (
                    [{"type": "text", "text": parts}]
                    if provider == "anthropic"
                    else [{"text": parts}]
                )
            existing.extend(parts)
            result[-1][key] = existing
        else:
            result.append({"role": native_role, key: parts})
    return result, "\n".join(systems)


async def stream_native_tools(
    config: ProviderConfig,
    messages: list[dict],
    *,
    tools: list[dict],
    max_tokens: int | None,
    temperature: float | None,
    top_p: float | None = None,
    emit_finish_reason: bool = False,
) -> AsyncIterator[
    str | ModelToolCall | ModelUsage | ModelFinish | ModelThought
]:
    history, instructions = native_messages(
        messages, config.provider, scope=provider_scope(config)
    )
    headers = {
        "accept": "text/event-stream",
        "content-type": "application/json",
    }
    if config.provider == "anthropic":
        headers.update(
            {"x-api-key": config.token, "anthropic-version": "2023-06-01"}
        )
        body = {
            "model": config.model,
            "stream": True,
            "messages": history,
            "max_tokens": max_tokens if max_tokens is not None else 16384,
            "tools": [
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"]["parameters"],
                }
                for t in tools
            ],
        }
        if instructions:
            body["system"] = instructions
        if not _anthropic_omits_sampling_parameters(config.model):
            if temperature is not None:
                body["temperature"] = temperature
            if top_p is not None:
                body["top_p"] = top_p
    elif config.provider == "google":
        headers["x-goog-api-key"] = config.token
        body = {
            "contents": history,
            "generationConfig": {},
            "tools": [
                {
                    "functionDeclarations": [
                        {
                            "name": t["function"]["name"],
                            "description": t["function"].get("description", ""),
                            "parametersJsonSchema": t["function"]["parameters"],
                        }
                        for t in tools
                    ]
                }
            ],
        }
        if instructions:
            body["systemInstruction"] = {"parts": [{"text": instructions}]}
        if max_tokens is not None:
            body["generationConfig"]["maxOutputTokens"] = max_tokens
        if temperature is not None:
            body["generationConfig"]["temperature"] = temperature
        if top_p is not None:
            body["generationConfig"]["topP"] = top_p
    else:
        raise ValueError("Unsupported model provider")
    pending: dict[int, dict] = {}
    usage_snapshot = None
    finish_reason = None
    terminal = False
    async with (
        provider_http_client(config) as client,
        client.stream(
            "POST",
            config.url,
            headers=headers,
            json=body,
        ) as response,
    ):
        response.raise_for_status()
        async for frame in _data_frames(response):
            if frame == "[DONE]":
                terminal = True
                break
            try:
                event = json.loads(frame)
            except json.JSONDecodeError as exc:
                raise ValueError("Invalid provider tool SSE frame") from exc
            if "error" in event or event.get("type") == "error":
                raise RuntimeError(
                    "Model provider rejected the generation request"
                )
            usage_snapshot = _usage_frame(
                config.provider, event, usage_snapshot
            )
            if config.provider == "anthropic":
                kind = event.get("type")
                if kind == "message_stop":
                    terminal = True
                if kind == "message_delta":
                    reason = (event.get("delta") or {}).get("stop_reason")
                    if isinstance(reason, str):
                        finish_reason = reason
                index = event.get("index")
                if kind == "content_block_start":
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        if (
                            type(index) is not int
                            or not 0 <= index < MAX_TOOL_CALLS
                            or index in pending
                        ):
                            raise ValueError("Invalid model tool call index")
                        pending[index] = {
                            "id": block.get("id"),
                            "name": block.get("name"),
                            "arguments": "",
                            "initial": block.get("input", {}),
                        }
                    elif block.get("type") == "redacted_thinking":
                        yield ModelThought(
                            "redacted_thinking",
                            data=block.get("data", ""),
                            provider_data={"scope": provider_scope(config)},
                        )
                elif kind == "content_block_delta":
                    delta = event.get("delta") or {}
                    change = delta.get("type")
                    if change == "text_delta" and isinstance(
                        delta.get("text"), str
                    ):
                        yield delta["text"]
                    elif change == "thinking_delta":
                        yield ModelThought(
                            "thinking", text=delta.get("thinking", "")
                        )
                    elif change == "signature_delta":
                        yield ModelThought(
                            "signature",
                            signature=delta.get("signature", ""),
                            provider_data={"scope": provider_scope(config)},
                        )
                    elif change == "input_json_delta":
                        if index not in pending:
                            raise ValueError(
                                "Tool arguments arrived before tool declaration"
                            )
                        pending[index]["arguments"] += (
                            delta.get("partial_json") or ""
                        )
                        if (
                            len(pending[index]["arguments"])
                            > MAX_TOOL_ARGUMENTS
                        ):
                            raise ValueError(
                                "Model tool arguments exceed size limit"
                            )
            else:
                for candidate in event.get("candidates", [])[:1]:
                    if isinstance(candidate.get("finishReason"), str):
                        finish_reason = candidate["finishReason"]
                    for part in (candidate.get("content") or {}).get(
                        "parts", []
                    ):
                        if isinstance(part.get("text"), str):
                            if part.get("thought"):
                                yield ModelThought(
                                    "thinking", text=part["text"]
                                )
                            else:
                                yield part["text"]
                        function = part.get("functionCall")
                        if function is not None:
                            if not isinstance(function, dict):
                                raise ValueError("Invalid model tool call")
                            if len(pending) >= MAX_TOOL_CALLS:
                                raise ValueError("Too many model tool calls")
                            pending[len(pending)] = {
                                "id": "call_" + secrets.token_hex(12),
                                "name": function.get("name"),
                                "initial": function.get("args", {}),
                                "arguments": "",
                                "metadata": {
                                    "thoughtSignature": part.get(
                                        "thoughtSignature"
                                    ),
                                    "scope": provider_scope(config),
                                },
                            }
    complete = finish_reason == (
        "tool_use" if config.provider == "anthropic" else "STOP"
    ) or (terminal and finish_reason is None)
    if pending and not complete:
        raise ValueError("Model stream ended without a complete tool batch")
    calls = []
    for item in pending.values():
        try:
            arguments = (
                json.loads(item["arguments"])
                if item["arguments"]
                else item["initial"]
            )
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid JSON in model tool call") from exc
        if (
            not isinstance(arguments, dict)
            or not isinstance(item["name"], str)
            or not item["name"]
            or not isinstance(item["id"], str)
            or not item["id"]
        ):
            raise ValueError("Invalid model tool call")
        calls.append(
            ModelToolCall(
                item["id"], item["name"], arguments, item.get("metadata")
            )
        )
    for call in complete_tool_batch(calls):
        yield call
    if usage_snapshot is not None:
        yield usage_snapshot
    if emit_finish_reason:
        yield ModelFinish(config.provider, finish_reason or "unknown")
