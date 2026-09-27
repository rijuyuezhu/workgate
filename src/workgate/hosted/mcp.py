"""Dependency-light stateless MCP gateway for hosted control actors."""

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
MODERN_MCP_PROTOCOL_VERSION = "2026-07-28"
_MODERN_MCP_PROTOCOL_VERSIONS = (MODERN_MCP_PROTOCOL_VERSION,)
_PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
_CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
_CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
_SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
_HEADER_MISMATCH = -32020
_UNSUPPORTED_PROTOCOL_VERSION = -32022


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
        self,
        payload: object,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, object] | None]:
        """Handle one stateless MCP Streamable-HTTP JSON-RPC message."""
        if not isinstance(payload, dict):
            return 400, _jsonrpc_error(None, -32600, "Invalid Request")
        method = payload.get("method")
        request_id = payload.get("id")
        has_request_id = "id" in payload
        if payload.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return 400, _jsonrpc_error(
                request_id if has_request_id else None,
                -32600,
                "Invalid Request",
            )
        if has_request_id and not (
            isinstance(request_id, str) or type(request_id) is int
        ):
            return 400, _jsonrpc_error(None, -32600, "Invalid Request")

        if _is_modern_request(payload, headers):
            if not has_request_id:
                return _acknowledge_modern_notification(headers)
            rejection = _validate_modern_request(payload, headers)
            if rejection is not None:
                return rejection
            return await self._handle_modern_request(
                method, request_id, payload
            )

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
            return await self._handle_tool_call(
                request_id, payload.get("params"), modern=False
            )

        return 200, _jsonrpc_error(
            request_id, -32601, f"Method not found: {method}"
        )

    async def _handle_modern_request(
        self,
        method: str,
        request_id: object,
        payload: Mapping[str, object],
    ) -> tuple[int, dict[str, object]]:
        """Handle one validated MCP 2026 request."""
        if method == "server/discover":
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    **_modern_cacheable_result(),
                    "supportedVersions": list(_MODERN_MCP_PROTOCOL_VERSIONS),
                    "capabilities": {"tools": {"listChanged": False}},
                    "instructions": (
                        "Workgate hosted control. Start machine work with "
                        "session_start and reuse its session_id."
                    ),
                },
            }

        if method == "tools/list":
            params = payload.get("params")
            if isinstance(params, dict) and params.get("cursor") is not None:
                return 400, _jsonrpc_error(
                    request_id,
                    -32602,
                    "hosted tools/list does not support pagination cursors",
                )
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    **_modern_cacheable_result(),
                    "tools": list(self.tools()),
                },
            }

        if method == "tools/call":
            params = payload.get("params")
            if isinstance(params, dict):
                unsupported = [
                    key
                    for key in ("requestState", "inputResponses", "task")
                    if params.get(key) is not None
                ]
                if unsupported:
                    return 400, _jsonrpc_error(
                        request_id,
                        -32602,
                        (
                            "hosted tools/call does not support: "
                            + ", ".join(unsupported)
                        ),
                    )
            return await self._handle_tool_call(request_id, params, modern=True)

        return 404, _jsonrpc_error(
            request_id, -32601, f"Method not found: {method}"
        )

    async def _handle_tool_call(
        self,
        request_id: object,
        params: object,
        *,
        modern: bool,
    ) -> tuple[int, dict[str, object]]:
        invalid_status = 400 if modern else 200
        if not isinstance(params, dict):
            return invalid_status, _jsonrpc_error(
                request_id, -32602, "Invalid params"
            )
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return invalid_status, _jsonrpc_error(
                request_id, -32602, "Invalid params"
            )
        common = _modern_result() if modern else {}
        try:
            result = await self.call_tool(name, arguments)
            structured = _jsonable(result)
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    **common,
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
                    **common,
                    "content": [
                        {
                            "type": "text",
                            "text": f"{type(exc).__name__}: {str(exc)[:500]}",
                        }
                    ],
                    "isError": True,
                },
            }


def _is_modern_request(
    payload: Mapping[str, object],
    headers: Mapping[str, str] | None,
) -> bool:
    """Distinguish MCP 2026 requests from handshake-era compatibility traffic."""
    if payload.get("method") == "server/discover":
        return True
    params = payload.get("params")
    if isinstance(params, dict):
        meta = params.get("_meta")
        if isinstance(meta, dict) and (
            _PROTOCOL_VERSION_META_KEY in meta
            or _CLIENT_CAPABILITIES_META_KEY in meta
        ):
            return True
    method_header = _header(headers, "mcp-method")
    name_header = _header(headers, "mcp-name")
    if method_header is not None or name_header is not None:
        return True
    version_header = _header(headers, "mcp-protocol-version")
    return bool(
        version_header and version_header not in SUPPORTED_MCP_PROTOCOL_VERSIONS
    )


def _validate_modern_request(
    payload: Mapping[str, object],
    headers: Mapping[str, str] | None,
) -> tuple[int, dict[str, object]] | None:
    """Validate the MCP 2026 per-request envelope and HTTP routing headers."""
    params_value = payload.get("params")
    params = params_value if isinstance(params_value, dict) else {}
    meta = params.get("_meta") if params else None
    if not isinstance(meta, dict):
        return 400, _jsonrpc_error(
            payload.get("id"),
            -32602,
            (
                "params._meta must be an object carrying the required "
                f"{_PROTOCOL_VERSION_META_KEY!r} and "
                f"{_CLIENT_CAPABILITIES_META_KEY!r} envelope keys"
            ),
        )
    missing = [
        key
        for key in (
            _PROTOCOL_VERSION_META_KEY,
            _CLIENT_CAPABILITIES_META_KEY,
        )
        if key not in meta
    ]
    if missing:
        return 400, _jsonrpc_error(
            payload.get("id"),
            -32602,
            (
                "params._meta is missing the required envelope key(s): "
                + ", ".join(missing)
            ),
        )
    version = meta[_PROTOCOL_VERSION_META_KEY]
    capabilities = meta[_CLIENT_CAPABILITIES_META_KEY]
    if not isinstance(version, str) or not isinstance(capabilities, dict):
        return 400, _jsonrpc_error(
            payload.get("id"),
            -32602,
            "invalid MCP 2026 request metadata",
        )
    client_info = meta.get(_CLIENT_INFO_META_KEY)
    if client_info is not None and not _valid_implementation(client_info):
        return 400, _jsonrpc_error(
            payload.get("id"),
            -32602,
            "invalid MCP 2026 clientInfo metadata",
        )

    method = payload.get("method")
    version_header = _header(headers, "mcp-protocol-version")
    method_header = _header(headers, "mcp-method")
    if version_header != version:
        return 400, _jsonrpc_error(
            payload.get("id"),
            _HEADER_MISMATCH,
            (
                "mcp-protocol-version header does not match the "
                "request body's protocol version"
            ),
        )
    if method_header != method:
        return 400, _jsonrpc_error(
            payload.get("id"),
            _HEADER_MISMATCH,
            "mcp-method header does not match the request body's method",
        )
    if method == "tools/call":
        name = params.get("name")
        if isinstance(name, str) and _header(headers, "mcp-name") != name:
            return 400, _jsonrpc_error(
                payload.get("id"),
                _HEADER_MISMATCH,
                (
                    "mcp-name header does not match the request body's "
                    "'name' parameter"
                ),
            )

    if version not in _MODERN_MCP_PROTOCOL_VERSIONS:
        return 400, _jsonrpc_error(
            payload.get("id"),
            _UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported protocol version",
            data={
                "supported": list(_MODERN_MCP_PROTOCOL_VERSIONS),
                "requested": version,
            },
        )
    return None


def _valid_implementation(value: object) -> bool:
    """Validate the dependency-light subset of the MCP Implementation shape."""
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get("name"), str) or not isinstance(
        value.get("version"), str
    ):
        return False
    for key in ("description", "title", "websiteUrl"):
        candidate = value.get(key)
        if candidate is not None and not isinstance(candidate, str):
            return False
    icons = value.get("icons")
    if icons is None:
        return True
    if not isinstance(icons, list):
        return False
    for icon in icons:
        if not isinstance(icon, dict) or not isinstance(icon.get("src"), str):
            return False
        mime_type = icon.get("mimeType")
        if mime_type is not None and not isinstance(mime_type, str):
            return False
        sizes = icon.get("sizes")
        if sizes is not None and (
            not isinstance(sizes, list)
            or any(not isinstance(size, str) for size in sizes)
        ):
            return False
        if icon.get("theme") not in {None, "dark", "light"}:
            return False
    return True


def _acknowledge_modern_notification(
    headers: Mapping[str, str] | None,
) -> tuple[int, dict[str, object] | None]:
    """Accept-and-drop one modern client notification at a served revision."""
    requested = _header(headers, "mcp-protocol-version")
    if requested not in _MODERN_MCP_PROTOCOL_VERSIONS:
        return 400, _jsonrpc_error(
            None,
            _UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported protocol version",
            data={
                "supported": list(_MODERN_MCP_PROTOCOL_VERSIONS),
                "requested": requested or "",
            },
        )
    return 202, None


def _header(
    headers: Mapping[str, str] | None,
    name: str,
) -> str | None:
    if headers is None:
        return None
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value)
    return None


def _modern_result() -> dict[str, object]:
    return {
        "_meta": {
            _SERVER_INFO_META_KEY: {
                "name": "workgate-hosted",
                "version": __version__,
            }
        },
        "resultType": "complete",
    }


def _modern_cacheable_result() -> dict[str, object]:
    return {
        **_modern_result(),
        "ttlMs": 0,
        "cacheScope": "private",
    }


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
    request_id: object,
    code: int,
    message: str,
    *,
    data: object | None = None,
) -> dict[str, object]:
    error: dict[str, object] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": error,
    }
