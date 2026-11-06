"""Call authenticated Control MCP tools directly from the WorkGate CLI."""

import argparse
import asyncio
import ipaddress
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

from ..config.settings import load_settings

_MAX_JSON_BYTES = 1024 * 1024


def _mcp_url(value: str) -> str:
    """Allow only unambiguous direct loopback Control addresses."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Control URL is invalid") from exc
    if host.lower() == "localhost":
        is_local = True
    else:
        try:
            is_local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_local = False
    if (
        parsed.scheme not in {"http", "https"}
        or not is_local
        or not parsed.netloc
        or "%" in parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/", "/mcp"}
        or parsed.netloc.endswith(":")
        or port == 0
    ):
        raise ValueError(
            "Control URL must be a direct loopback HTTP(S) MCP endpoint"
        )
    return f"{parsed.scheme}://{parsed.netloc}/mcp"


def _arguments(raw: str | None, session_id: str | None) -> dict[str, Any]:
    input_text = (
        raw
        if raw is not None
        else (
            "{}" if sys.stdin.isatty() else sys.stdin.read(_MAX_JSON_BYTES + 1)
        )
    )
    if raw is None and not input_text.strip():
        input_text = "{}"
    if len(input_text.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError("tool JSON exceeds 1 MiB")

    def reject_constant(value: str) -> None:
        raise ValueError(f"{value} is not valid JSON")

    try:
        parsed = json.loads(input_text, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError("tool arguments must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    if session_id is not None:
        if "session_id" in parsed and parsed["session_id"] != session_id:
            raise ValueError("--session conflicts with JSON session_id")
        parsed["session_id"] = session_id
    return parsed


def _read_token(path: str | None) -> str | None:
    if path is None:
        value = os.environ.get("WORKGATE_CALL_TOKEN") or None
    else:
        candidate = Path(path).expanduser()
        if candidate.is_symlink():
            raise ValueError("token file must be a regular file, not a symlink")
        descriptor = os.open(
            candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("token file must be a regular file")
            if os.name != "nt" and info.st_mode & 0o077:
                raise ValueError(
                    "token file must not be readable by group or others"
                )
            if info.st_size > 64 * 1024:
                raise ValueError("token file is too large")
            value = handle.read(64 * 1024 + 1).decode("utf-8").strip()
    if value is not None and (
        not value
        or len(value) > 64 * 1024
        or any(char.isspace() for char in value)
    ):
        raise ValueError("token must contain one OAuth access token")
    return value


async def _call(
    url: str,
    tool: str,
    arguments: dict[str, Any],
    token: str | None,
    timeout_s: float,
) -> CallToolResult:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with (
        httpx2.AsyncClient(
            headers=headers,
            follow_redirects=False,
            trust_env=False,
            timeout=timeout_s,
        ) as client,
        streamable_http_client(url, http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        outcome = await session.call_tool(
            tool, arguments, read_timeout_seconds=timeout_s
        )
    if not isinstance(outcome, CallToolResult):
        raise RuntimeError("Control returned an unsupported MCP tool result")
    return outcome


def run_call_from_args(args: argparse.Namespace) -> None:
    """Return structured result JSON and a conventional command exit status."""
    try:
        if args.url is None:
            settings = load_settings(
                args.config, setting_names={"host", "port"}
            )
            host = settings.host
            if host in {"0.0.0.0", "::", "[::]"}:
                host = "127.0.0.1"
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            selected = f"http://{host}:{settings.port}/mcp"
        else:
            selected = args.url
        url = _mcp_url(selected)
        values = _arguments(args.json_arguments, args.session)
        token = _read_token(args.token_file)
        if not (0 < args.timeout <= 3600):
            raise ValueError(
                "--timeout must be greater than 0 and at most 3600 seconds"
            )
        outcome = asyncio.run(
            _call(url, args.tool, values, token, args.timeout)
        )
    except Exception as exc:
        # Third-party transport errors may echo URL/headers; never print them.
        print(
            f"workgate call: {type(exc).__name__}; check Control address and authentication",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    has_nontext_content = any(item.type != "text" for item in outcome.content)
    value = (
        outcome.structured_content
        if not outcome.is_error
        and outcome.structured_content is not None
        and not has_nontext_content
        else outcome.model_dump(mode="json", by_alias=True, exclude_none=True)
    )
    print(json.dumps(value, ensure_ascii=False))
    if outcome.is_error:
        raise SystemExit(1)


def register_call_cli(subparsers: Any) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "call", help="Invoke an authenticated tool on a running Control"
    )
    parser.add_argument("tool", help="Registered MCP tool name")
    parser.add_argument(
        "--json", dest="json_arguments", help="JSON tool arguments (or stdin)"
    )
    parser.add_argument(
        "--session", help="Set session_id for session-scoped tools"
    )
    parser.add_argument("--url", help="Loopback HTTP(S) Control MCP URL")
    parser.add_argument(
        "--config", help="Read local Control address from this config"
    )
    parser.add_argument(
        "--token-file", help="Private file holding an OAuth bearer token"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="MCP call timeout in seconds",
    )
    parser.set_defaults(handler=run_call_from_args)
    return parser
