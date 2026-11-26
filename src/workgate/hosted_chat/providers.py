"""Final model streaming adapters, using explicit Workgate provider profiles."""

import json
from collections.abc import AsyncIterator
from urllib.parse import quote, urlsplit

from .mcp_projection import openai_wire_messages
from .network import validate_provider_endpoint
from .provider_common import (
    MAX_TOOL_ARGUMENTS,
    MAX_TOOL_CALLS,
    _anthropic_omits_sampling_parameters,
    _data_frames,
    _openai_reasoning_fragments,
    _provider_stream,
    _usage_frame,
    complete_tool_batch,
    provider_http_client,
    provider_scope,
)
from .provider_common import (
    ModelFinish as ModelFinish,
)
from .provider_common import (
    ModelThought as ModelThought,
)
from .provider_common import (
    ModelToolCall as ModelToolCall,
)
from .provider_common import (
    ModelUsage as ModelUsage,
)
from .provider_common import (
    ProviderConfig as ProviderConfig,
)
from .provider_profiles import ProviderRepository
from .provider_responses import stream_responses
from .provider_tool_protocols import native_messages, stream_native_tools


def selected_provider(payload: dict, agent: dict | None = None) -> str:
    """Use the exact configured endpoint identity for history and Agents."""
    selected = (agent or {}).get("provider") or payload.get("provider")
    if selected:
        return str(selected)
    endpoint = payload.get("endpoint")
    return (
        str(endpoint)
        if isinstance(endpoint, str) and endpoint not in {"agents", ""}
        else "openai"
    )


def provider_config(
    repo: ProviderRepository, provider: str, model: str | None = None
) -> ProviderConfig:
    value = repo.profile(provider)
    kind = value["provider"]
    token, base = value.get("api_key", ""), value["base_url"]
    private_http = value.get("allow_private_http", False)
    validate_provider_endpoint(base, allow_private_http=private_http)
    if value.get("oauth_id"):
        raise ValueError("Account authorization service is not available")
    if not model and not value["models"]:
        raise ValueError("No available models in this channel")
    name = model or value["models"][0]
    if kind in {"openai", "openai-responses"}:
        if (
            not token
            and not value.get("keyless")
            and urlsplit(base).hostname not in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("An OpenAI-compatible model API key is required")
        url = base.rstrip("/") + (
            "/responses" if kind == "openai-responses" else "/chat/completions"
        )
    elif kind == "anthropic":
        if not token:
            raise ValueError("An Anthropic model API key is required")
        url = base.rstrip("/") + "/messages"
    elif kind == "google":
        if not token:
            raise ValueError("A Gemini model API key is required")
        if any(
            char
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
            for char in name
        ):
            raise ValueError("Unsupported Gemini model identifier")
        url = (
            base.rstrip("/")
            + f"/models/{quote(name)}:streamGenerateContent?alt=sse"
        )
    else:
        raise ValueError("Unsupported model provider")
    return ProviderConfig(
        kind,
        name,
        url,
        token,
        sampling=(value.get("model_sampling") or {}).get(name, {}),
        account_channel=value.get("name") if value.get("oauth_id") else None,
        account_id=value.get("oauth_id"),
        auth_method=value.get("auth_method"),
        channel=value.get("name", kind),
        allow_private_http=private_http,
    )


async def stream_model(
    config: ProviderConfig,
    messages: list[dict],
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    emit_finish_reason: bool = False,
) -> AsyncIterator[str | ModelUsage | ModelFinish | ModelThought]:
    """Stream user-visible answer text without buffering the provider response."""
    if config.provider == "openai-responses":
        async for event in stream_responses(
            config,
            messages,
            tools=[],
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            emit_finish_reason=emit_finish_reason,
        ):
            yield event
        return

    headers = {
        "accept": "text/event-stream",
        "content-type": "application/json",
    }
    body: dict = {"model": config.model, "stream": True}
    if config.provider == "openai":
        if config.token:
            headers["Authorization"] = f"Bearer {config.token}"
        # Local OpenAI-compatible servers may run without an API key. An
        # empty `Bearer ` value is illegal HTTP and breaks streaming.
        body["messages"] = openai_wire_messages(messages)
        if max_tokens is not None:
            body["max_completion_tokens"] = max_tokens
        body["stream_options"] = {"include_usage": True}
        if temperature is not None:
            body["temperature"] = temperature
        if top_p is not None:
            body["top_p"] = top_p
    elif config.provider == "anthropic":
        headers["x-api-key"] = config.token
        headers["anthropic-version"] = "2023-06-01"
        items, instructions = native_messages(
            messages, "anthropic", scope=provider_scope(config)
        )
        body = {
            "model": config.model,
            "messages": items,
            "max_tokens": max_tokens if max_tokens is not None else 16384,
            "stream": True,
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
        items, instructions = native_messages(
            messages, "google", scope=provider_scope(config)
        )
        body = {"contents": items, "generationConfig": {}}
        if max_tokens is not None:
            body["generationConfig"]["maxOutputTokens"] = max_tokens
        if instructions:
            body["systemInstruction"] = {"parts": [{"text": instructions}]}
        if temperature is not None:
            body["generationConfig"]["temperature"] = temperature
        if top_p is not None:
            body["generationConfig"]["topP"] = top_p
    else:
        raise ValueError("Unsupported model provider")
    usage_snapshot = None
    finish_reason = None
    async with (
        provider_http_client(config) as http,
        _provider_stream(
            http, config.url, body=body, headers=headers
        ) as response,
    ):
        async for frame in _data_frames(response):
            if frame == "[DONE]":
                break
            try:
                event = json.loads(frame)
            except json.JSONDecodeError:
                continue
            if "error" in event:
                raise RuntimeError(
                    "Model provider rejected the generation request"
                )
            usage_snapshot = _usage_frame(
                config.provider, event, usage_snapshot
            )
            if config.provider == "openai":
                for choice in event.get("choices", []):
                    if isinstance(choice.get("finish_reason"), str):
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    for thought in _openai_reasoning_fragments(delta):
                        yield ModelThought("thinking", text=thought)
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        yield content
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and isinstance(
                                part.get("text"), str
                            ):
                                yield part["text"]
            elif config.provider == "anthropic":
                if event.get("type") == "message_delta":
                    reason = (event.get("delta") or {}).get("stop_reason")
                    if isinstance(reason, str):
                        finish_reason = reason
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "thinking_delta" and isinstance(
                        delta.get("thinking"), str
                    ):
                        yield ModelThought("thinking", text=delta["thinking"])
                    elif delta.get("type") == "signature_delta" and isinstance(
                        delta.get("signature"), str
                    ):
                        yield ModelThought(
                            "signature",
                            signature=delta["signature"],
                            provider_data={"scope": provider_scope(config)},
                        )
                    text = delta.get("text")
                    if isinstance(text, str) and text:
                        yield text
            else:
                for candidate in event.get("candidates", []):
                    if isinstance(candidate.get("finishReason"), str):
                        finish_reason = candidate["finishReason"]
                    for part in (candidate.get("content") or {}).get(
                        "parts", []
                    ):
                        text = part.get("text")
                        if isinstance(text, str) and text:
                            if part.get("thought"):
                                yield ModelThought("thinking", text=text)
                            else:
                                yield text

    if usage_snapshot is not None:
        yield usage_snapshot
    if emit_finish_reason:
        yield ModelFinish(config.provider, finish_reason or "unknown")


async def stream_model_with_tools(
    config: ProviderConfig,
    messages: list[dict],
    *,
    tools: list[dict],
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
) -> AsyncIterator[
    str | ModelToolCall | ModelUsage | ModelThought | ModelFinish
]:
    """Stream text and complete, validated tool proposals for each protocol."""
    if config.provider == "openai-responses":
        async for event in stream_responses(
            config,
            messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
        ):
            yield event
        return
    if tools and config.provider in {"anthropic", "google"}:
        async for event in stream_native_tools(
            config,
            messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
        ):
            yield event
        return
    if not tools or config.provider != "openai":
        async for event in stream_model(
            config,
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
        ):
            yield event
        return

    messages = openai_wire_messages(messages)
    headers = {
        "accept": "text/event-stream",
        "content-type": "application/json",
    }
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"
    body: dict = {
        "model": config.model,
        "stream": True,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    if max_tokens is not None:
        body["max_completion_tokens"] = max_tokens
    body["stream_options"] = {"include_usage": True}
    if temperature is not None:
        body["temperature"] = temperature
    if top_p is not None:
        body["top_p"] = top_p
    pending: dict[int, dict[str, str]] = {}
    terminal = False
    finish_reason = None
    usage_snapshot = None
    async with (
        provider_http_client(config) as client,
        _provider_stream(
            client, config.url, body=body, headers=headers
        ) as response,
    ):
        async for frame in _data_frames(response):
            if frame == "[DONE]":
                terminal = True
                break
            try:
                event = json.loads(frame)
            except json.JSONDecodeError as exc:
                raise ValueError("Invalid provider tool SSE frame") from exc
            if "error" in event:
                raise RuntimeError(
                    "Model provider rejected the generation request"
                )
            usage_snapshot = _usage_frame(
                config.provider, event, usage_snapshot
            )
            for choice in event.get("choices", [])[:1]:
                if isinstance(choice.get("finish_reason"), str):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}
                for thought in _openai_reasoning_fragments(delta):
                    yield ModelThought("thinking", text=thought)
                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield content
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and isinstance(
                            part.get("text"), str
                        ):
                            yield part["text"]
                for fragment in delta.get("tool_calls") or []:
                    index = fragment.get("index")
                    if (
                        type(index) is not int
                        or not 0 <= index < MAX_TOOL_CALLS
                    ):
                        raise ValueError("Invalid model tool call index")
                    tool = pending.setdefault(
                        index, {"id": "", "name": "", "arguments": ""}
                    )
                    if fragment.get("id"):
                        tool["id"] += str(fragment["id"])
                    function = fragment.get("function") or {}
                    tool["name"] += str(function.get("name") or "")
                    tool["arguments"] += str(function.get("arguments") or "")
                    if len(tool["arguments"]) > MAX_TOOL_ARGUMENTS:
                        raise ValueError(
                            "Model tool arguments exceed size limit"
                        )
    if pending and not (
        finish_reason == "tool_calls" or (terminal and finish_reason is None)
    ):
        raise ValueError("Model stream ended without a complete tool batch")
    calls = []
    for index in sorted(pending):
        tool = pending[index]
        if not tool["id"] or not tool["name"]:
            raise ValueError("Incomplete model tool call")
        try:
            params = json.loads(tool["arguments"] or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON in model tool call") from exc
        if not isinstance(params, dict):
            raise ValueError("Model tool arguments must be an object")
        calls.append(ModelToolCall(tool["id"], tool["name"], params))
    for call in complete_tool_batch(calls):
        yield call
    if usage_snapshot is not None:
        yield usage_snapshot
