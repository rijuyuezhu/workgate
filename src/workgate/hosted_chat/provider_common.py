"""Shared final provider types, SSE framing and request primitives.

Extracted from pinned upstream providers.py to preserve a one-way dependency
graph between the provider protocol adapters.
"""

import hashlib
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx

from .network import provider_client

MAX_TOOL_ARGUMENTS = 1024 * 1024
MAX_TOOL_CALLS = 128
MAX_SSE_FRAME_CHARS = 8 * 1024 * 1024


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str
    url: str
    token: str = field(repr=False)
    timeout_s: float = 180.0
    sampling: dict = field(default_factory=dict)
    account_channel: str | None = None
    account_id: str | None = None
    auth_method: str | None = None
    channel: str | None = None
    allow_private_http: bool = False


def provider_scope(config: ProviderConfig) -> str:
    """Bind opaque replay metadata to the originating channel and credential."""
    return hashlib.sha256(
        json.dumps(
            [
                config.channel,
                config.provider,
                config.url,
                config.model,
                config.account_id or config.token,
            ]
        ).encode()
    ).hexdigest()


def provider_http_client(config: ProviderConfig) -> httpx.AsyncClient:
    if config.account_id or config.auth_method:
        raise ValueError("Account authorization service is not available")
    url = urlsplit(config.url)
    if url.fragment or (
        url.query and (config.provider != "google" or url.query != "alt=sse")
    ):
        raise ValueError("Invalid provider request URL")
    base = urlunsplit((url.scheme, url.netloc, url.path, "", ""))
    timeout = httpx.Timeout(
        connect=20, read=config.timeout_s, write=30, pool=10
    )
    return provider_client(
        base, allow_private_http=config.allow_private_http, timeout=timeout
    )


async def _data_frames(response) -> AsyncIterator[str]:
    """Parse multi-line SSE data frames, ignoring comments and heartbeat lines."""
    data: list[str] = []
    size = 0
    async for raw in response.aiter_lines():
        line = raw.rstrip("\r")
        if line == "":
            if data:
                yield "\n".join(data)
                data.clear()
                size = 0
        elif line.startswith("data:"):
            part = line[5:].lstrip(" ")
            size += len(part) + 1
            if size > MAX_SSE_FRAME_CHARS:
                raise ValueError("Provider SSE frame exceeds size limit")
            data.append(part)
    if data:
        yield "\n".join(data)


@asynccontextmanager
async def _provider_stream(client, url: str, *, body: dict, headers: dict):
    """Fallback only for a rejected streaming-usage option before any output."""
    for attempt in range(2):
        async with client.stream(
            "POST", url, json=body, headers=headers
        ) as response:
            if (
                attempt == 0
                and response.status_code in {400, 422}
                and "stream_options" in body
            ):
                problem = bytearray()
                async for chunk in response.aiter_bytes():
                    problem.extend(chunk[: 4096 - len(problem)])
                    if len(problem) >= 4096:
                        break
                lower = problem.lower()
                if (
                    b"stream_options" in lower or b"include_usage" in lower
                ) and any(
                    word in lower
                    for word in (
                        b"unsupported",
                        b"not support",
                        b"unknown",
                        b"unrecognized",
                        b"not allowed",
                        b"extra",
                    )
                ):
                    body = {
                        key: value
                        for key, value in body.items()
                        if key != "stream_options"
                    }
                    continue
            response.raise_for_status()
            yield response
            return
    raise RuntimeError("Model provider rejected usage fallback")


def _anthropic_omits_sampling_parameters(model: str) -> bool:
    """Match LibreChat model-aware sampling restrictions for newer Claude models.

    Builder hides incompatible controls but retains saved Agent parameters.
    Accept both Claude version naming formats; do not parse model dates as minors.
    """
    if re.search(r"claude-(?:fable|mythos)[-.]?\d", model):
        return True
    for family, minimum in (
        ("opus", (4, 7)),
        ("sonnet", (5, 0)),
        ("haiku", (5, 5)),
    ):
        expressions = (
            rf"claude-{family}[-.]?(\d+)(?:[-.](\d{{1,2}})(?!\d))?",
            rf"claude-(\d+)(?:[-.](\d{{1,2}})(?!\d))?-{family}",
        )
        for expression in expressions:
            match = re.search(expression, model)
            if match and (int(match[1]), int(match[2] or 0)) >= minimum:
                return True
    return False


@dataclass(frozen=True)
class ModelFinish:
    """Provider-reported stop reason, emitted only to explicit summary callers."""

    provider: str
    reason: str


@dataclass(frozen=True)
class ModelUsage:
    """Confirmed provider token counts, never a local token estimate."""

    provider: str
    input_tokens: int
    output_tokens: int


def _usage_frame(
    provider: str, event: dict, previous: ModelUsage | None = None
) -> ModelUsage | None:
    if provider == "openai":
        usage = event.get("usage")
        if not isinstance(usage, dict):
            return previous
        raw_input = usage.get("prompt_tokens", usage.get("input_tokens"))
        raw_output = usage.get("completion_tokens", usage.get("output_tokens"))
    elif provider == "anthropic":
        kind = event.get("type")
        if kind == "message_start":
            metadata = event.get("message")
            usage = (
                metadata.get("usage") if isinstance(metadata, dict) else None
            )
        elif kind == "message_delta":
            usage = event.get("usage")
        else:
            return previous
        if not isinstance(usage, dict):
            return previous
        raw_input = usage.get(
            "input_tokens", previous.input_tokens if previous else None
        )
        if (
            "input_tokens" in usage
            and kind == "message_start"
            and type(raw_input) is int
        ):
            # Anthropic reports cache reads/writes separately from ordinary
            # input_tokens. Counts include all provider-confirmed input, while
            # cache billing remains an independent accounting problem.
            cached_read = usage.get("cache_read_input_tokens", 0)
            cached_write = usage.get("cache_creation_input_tokens", 0)
            if type(cached_read) is not int or type(cached_write) is not int:
                return previous
            raw_input += cached_read + cached_write
        raw_output = usage.get(
            "output_tokens", previous.output_tokens if previous else None
        )
    else:
        usage = event.get("usageMetadata")
        if not isinstance(usage, dict):
            return previous
        raw_input = usage.get("promptTokenCount")
        raw_output = usage.get("candidatesTokenCount")
    if (
        type(raw_input) is not int
        or raw_input < 0
        or raw_input > 10**12
        or type(raw_output) is not int
        or raw_output < 0
        or raw_output > 10**12
    ):
        return previous
    return ModelUsage(provider, raw_input, raw_output)


def _openai_reasoning_fragments(delta: dict) -> list[str]:
    """Extract visible reasoning from common OpenAI-compatible chat deltas.

    Prefer the single text field when vendors also send structured copies;
    encrypted reasoning, signatures and token counts are not readable text.
    """
    for key in ("reasoning_content", "reasoning"):
        value = delta.get(key)
        if isinstance(value, str) and value:
            return [value]
    details = delta.get("reasoning_details")
    if not isinstance(details, list):
        return []
    return [
        part["text"]
        for part in details
        if isinstance(part, dict)
        and part.get("type") in {"reasoning.text", "text"}
        and isinstance(part.get("text"), str)
        and part["text"]
    ]


@dataclass(frozen=True)
class ModelThought:
    """Provider reasoning event stored in canonical history, never UI answer text."""

    kind: str
    text: str = ""
    signature: str = ""
    data: str = ""
    provider_data: dict | None = None


@dataclass(frozen=True)
class ModelToolCall:
    """A complete model-requested function call, never partial SSE input."""

    call_id: str
    name: str
    arguments: dict
    provider_data: dict | None = None

    def history_metadata(self) -> dict:
        return (
            {"provider_data": self.provider_data} if self.provider_data else {}
        )


def complete_tool_batch(calls: list[ModelToolCall]) -> list[ModelToolCall]:
    """Validate the whole proposal before releasing any tool calls."""
    ids = set()
    if len(calls) > MAX_TOOL_CALLS:
        raise ValueError("Too many model tool calls")
    for call in calls:
        if (
            not isinstance(call.call_id, str)
            or not call.call_id
            or len(call.call_id) > 1024
            or call.call_id in ids
        ):
            raise ValueError("Invalid or duplicate model tool call ID")
        if (
            not isinstance(call.name, str)
            or not call.name
            or len(call.name) > 1024
        ):
            raise ValueError("Invalid model tool name")
        if (
            not isinstance(call.arguments, dict)
            or len(json.dumps(call.arguments)) > MAX_TOOL_ARGUMENTS
        ):
            raise ValueError("Invalid or oversized model tool arguments")
        ids.add(call.call_id)
    return calls
