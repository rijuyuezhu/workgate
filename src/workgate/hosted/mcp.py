"""Dependency-light stateless MCP gateway for hosted control actors."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any, cast

from .. import __version__
from ._tool_manifest import (
    HOSTED_TOOL_MANIFEST,
    LATEST_MCP_PROTOCOL_VERSION,
    SUPPORTED_MCP_PROTOCOL_VERSIONS,
)
from .actor import HostedControlActorCore

HOSTED_MCP_TOOL_NAMES = frozenset(
    str(tool["name"]) for tool in HOSTED_TOOL_MANIFEST
)
_HOSTED_TOOL_INPUT_SCHEMAS = {
    str(tool["name"]): tool["inputSchema"] for tool in HOSTED_TOOL_MANIFEST
}


class HostedMcpGateway:
    """Expose the supported hosted tool subset through stateless MCP JSON-RPC."""

    def __init__(self, actor: HostedControlActorCore) -> None:
        self._actor = actor

    @staticmethod
    def tools() -> tuple[dict[str, object], ...]:
        """Return the generated canonical MCP tool manifest."""
        return HOSTED_TOOL_MANIFEST

    async def call_tool(self, name: str, args: Mapping[str, Any]) -> Any:
        """Route one supported hosted tool call without importing executor code."""
        if name not in HOSTED_MCP_TOOL_NAMES:
            raise ValueError(f"unsupported hosted MCP tool: {name}")
        payload = dict(args)
        _validate_json_schema(
            payload, _HOSTED_TOOL_INPUT_SCHEMAS[name], path="arguments"
        )
        if (
            name in {"bash", "run_python_code"}
            and payload.get("async_") is True
            and payload.get("pty") is not True
        ):
            raise ValueError(
                "background job creation is not supported by the hosted adapter yet"
            )
        sessions = self._actor.session_coordinator
        if name == "session_start":
            return await sessions.start_session(
                workdir=str(payload["workdir"]),
                label=payload.get("label"),
                executor_id=payload.get("executor_id"),
            )
        if name == "session_change_cwd":
            return await sessions.change_cwd(
                str(payload["session_id"]), str(payload["workdir"])
            )
        if name == "session_end":
            return await sessions.end_session(
                str(payload["session_id"]),
                force=bool(payload.get("force", False)),
            )
        return await sessions.call_session_tool(name, payload)

    async def handle_jsonrpc(
        self, payload: object
    ) -> tuple[int, dict[str, object] | None]:
        """Handle one stateless MCP Streamable-HTTP JSON-RPC message."""
        if not isinstance(payload, dict):
            return 400, _jsonrpc_error(None, -32600, "Invalid Request")
        method = payload.get("method")
        request_id = payload.get("id")

        if request_id is None:
            if method == "notifications/initialized":
                return 202, None
            return 202, None

        if method == "initialize":
            params = payload.get("params")
            requested = (
                params.get("protocolVersion")
                if isinstance(params, dict)
                else None
            )
            version = (
                str(requested)
                if requested in SUPPORTED_MCP_PROTOCOL_VERSIONS
                else LATEST_MCP_PROTOCOL_VERSION
            )
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "workgate-hosted",
                        "version": __version__,
                    },
                    "instructions": (
                        "Workgate hosted control. Start machine work with "
                        "session_start and reuse its session_id."
                    ),
                },
            }

        if method == "ping":
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {},
            }

        if method == "tools/list":
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": list(self.tools())},
            }

        if method == "tools/call":
            params = payload.get("params")
            if not isinstance(params, dict):
                return 200, _jsonrpc_error(request_id, -32602, "Invalid params")
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return 200, _jsonrpc_error(request_id, -32602, "Invalid params")
            try:
                result = await self.call_tool(name, arguments)
                structured = _jsonable(result)
                return 200, {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    structured,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                            }
                        ],
                        "structuredContent": structured,
                        "isError": False,
                    },
                }
            except Exception as exc:
                return 200, {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"{type(exc).__name__}: {str(exc)[:500]}"
                                ),
                            }
                        ],
                        "isError": True,
                    },
                }

        return 200, _jsonrpc_error(
            request_id, -32601, f"Method not found: {method}"
        )


def _validate_json_schema(value: Any, schema: object, *, path: str) -> None:
    """Validate the small JSON Schema subset emitted by canonical MCP tools."""
    if not isinstance(schema, dict):
        raise ValueError(f"invalid hosted schema at {path}")

    variants = schema.get("anyOf")
    if isinstance(variants, list):
        for variant in variants:
            try:
                _validate_json_schema(value, variant, path=path)
            except ValueError:
                continue
            return
        raise ValueError(
            f"invalid arguments at {path}: no allowed schema matched"
        )

    expected = schema.get("type")
    if isinstance(expected, str) and not _matches_json_type(value, expected):
        raise ValueError(
            f"invalid arguments at {path}: expected {expected}, "
            f"got {_json_type_name(value)}"
        )

    if expected == "object" and isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    raise ValueError(
                        f"invalid arguments at {path}: missing required {key!r}"
                    )
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            properties = {}
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                _validate_json_schema(
                    item, properties[key], path=f"{path}.{key}"
                )
            elif additional is False:
                raise ValueError(
                    f"invalid arguments at {path}: unexpected property {key!r}"
                )
            elif isinstance(additional, dict):
                _validate_json_schema(item, additional, path=f"{path}.{key}")

    if expected == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_json_schema(
                    item, item_schema, path=f"{path}[{index}]"
                )

    if expected == "string" and isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        pattern = schema.get("pattern")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(
                f"invalid arguments at {path}: string is shorter than {minimum}"
            )
        if isinstance(maximum, int) and len(value) > maximum:
            raise ValueError(
                f"invalid arguments at {path}: string is longer than {maximum}"
            )
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise ValueError(
                f"invalid arguments at {path}: string does not match required pattern"
            )

    if expected in {"integer", "number"} and _is_json_number(value):
        numeric_value = cast(int | float, value)
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, int | float) and numeric_value < minimum:
            raise ValueError(
                f"invalid arguments at {path}: value is below {minimum}"
            )
        if isinstance(maximum, int | float) and numeric_value > maximum:
            raise ValueError(
                f"invalid arguments at {path}: value is above {maximum}"
            )


def _matches_json_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "integer":
        return _is_json_number(value) and float(value).is_integer()
    if expected == "number":
        return _is_json_number(value)
    raise ValueError(f"unsupported hosted JSON Schema type: {expected}")


def _is_json_number(value: Any) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if _is_json_number(value):
        return "number"
    return type(value).__name__


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    json.dumps(value, ensure_ascii=False, allow_nan=False)
    return value


def _jsonrpc_error(
    request_id: object, code: int, message: str
) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
