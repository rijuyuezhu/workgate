"""LibreChat ActionRequest/RequestExecutor behavior with connect-pinned SSRF checks.

This is a distinct HTTP Action backend, not a new LSM native or MCP tool.
Model tools remain subject to Agent selection and the runner's approval gate.
"""

import asyncio
import base64
import ipaddress
import json
import os
import re
import socket
from urllib.parse import quote, urlsplit, urlunsplit

import httpcore
import httpx

from .action_spec import METHODS, _validate_tree
from .actions import ActionRepository
from .network import _PRIVATE_NETWORKS, ProviderHTTPTransport

_MAX_RESPONSE = 256 * 1024
_TIMEOUT_S = 20.0
_MAX_REQUEST = 256 * 1024


def _allowed_local(host: str, port: int) -> bool:
    # Operator-only, exact host:port allowlist; never read from Action metadata.
    allowed = {
        item.strip().lower()
        for item in os.getenv("WORKGATE_ACTION_ALLOWED_ADDRESSES", "").split(
            ","
        )
    }
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return authority.lower() in allowed


def _allowed_domain(host: str) -> bool:
    allowed = {
        item.strip().lower()
        for item in os.getenv("WORKGATE_ACTION_ALLOWED_DOMAINS", "").split(",")
        if item.strip()
    }
    return not allowed or host.lower() in allowed


def _allowed_ip(value: str, *, local: bool) -> bool:
    if "%" in value:
        return False
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if (
        address.is_unspecified
        or address.is_multicast
        or address.is_link_local
        or address.is_reserved
        or address == ipaddress.ip_address("100.100.100.200")
    ):
        return False
    return address.is_global or (
        local
        and (
            address.is_loopback
            or any(address in net for net in _PRIVATE_NETWORKS)
        )
    )


def _resolve_addresses(host: str, port: int) -> list[str]:
    if not _allowed_domain(host):
        raise httpcore.ConnectError("Action domain not allowed")
    try:
        addresses = list(
            dict.fromkeys(
                str(answer[4][0])
                for answer in socket.getaddrinfo(
                    host, port, type=socket.SOCK_STREAM
                )
                if answer[0] in {socket.AF_INET, socket.AF_INET6}
            )
        )
        if addresses and all(
            _allowed_ip(ip, local=_allowed_local(host, port))
            for ip in addresses
        ):
            return addresses
    except OSError, ValueError:
        pass
    raise httpcore.ConnectError(
        "Action destination is unresolved or outside network policy"
    )


class ActionHTTPTransport(ProviderHTTPTransport):
    """Share the origin-pinned dialer, with a separate Action destination policy."""

    def __init__(self, server: str):
        parsed = urlsplit(server)
        if (
            not parsed.hostname
            or parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.port == 0
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in server)
            or "\\" in server
            or "%" in parsed.hostname
        ):
            raise ValueError("Invalid Action server URL")
        self._initialize(server, resolve=_resolve_addresses)


def _validate(
    value: object, schema: dict, *, prefix: str = "arguments"
) -> None:
    typ = schema.get("type")
    if typ == "object":
        if not isinstance(value, dict):
            raise ValueError(prefix + " must be an object")
        for name in schema.get("required") or []:
            if name not in value:
                raise ValueError(prefix + " is missing required parameter")
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False and set(value) - set(
            properties
        ):
            raise ValueError(prefix + " contains unknown parameters")
        for key, item in value.items():
            if key in properties and isinstance(properties[key], dict):
                _validate(item, properties[key], prefix=prefix + "." + key)
    elif typ == "array":
        if not isinstance(value, list):
            raise ValueError(prefix + " must be an array")
        for element in value:
            _validate(element, schema.get("items") or {}, prefix=prefix + "[]")
    elif typ == "string":
        if not isinstance(value, str):
            raise ValueError(prefix + " must be a string")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get(
            "maxLength", 100_000
        ):
            raise ValueError(prefix + " invalid string length")
    elif typ in ("integer", "number"):
        if type(value) not in (float, int) or (
            typ == "integer" and not isinstance(value, int)
        ):
            raise ValueError(prefix + " must be numeric")
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(prefix + " is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(prefix + " is above maximum")
    elif typ == "boolean" and not isinstance(value, bool):
        raise ValueError(prefix + " must be boolean")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(prefix + " invalid enum member")


def _request(op: dict, args: dict) -> tuple[str, dict, dict, dict]:
    if op["method"] not in METHODS:
        raise ValueError("Unsupported HTTP method")
    _validate_tree(args)
    if len(json.dumps(args, ensure_ascii=False).encode()) > _MAX_REQUEST:
        raise ValueError("Action request exceeds size limit")
    _validate(args, op["parameters"])
    path = op["path"]
    values = dict(args)
    for key, val in args.items():
        if op["locations"].get(key) == "path":
            needle = "{" + key + "}"
            if needle in path:
                path = path.replace(needle, quote(str(val), safe=""))
                values.pop(key, None)
    if "{" in path or "}" in path:
        raise ValueError("Missing required path parameter")
    query, body, headers = {}, {}, {}
    for key, val in values.items():
        place = op["locations"].get(
            key, "query" if op["method"] == "get" else "body"
        )
        if place == "query":
            query[key] = val
        elif place == "body":
            body[key] = val
        elif place == "header":
            if not re.fullmatch(r"[A-Za-z0-9-]+", key) or key.lower() in {
                "authorization",
                "host",
                "cookie",
                "content-length",
                "connection",
                "transfer-encoding",
                "proxy-authorization",
                "accept-encoding",
                "content-type",
                "upgrade",
                "te",
                "trailer",
                "expect",
            }:
                raise ValueError("Reserved Action request header")
            if not isinstance(val, (str, int, float, bool)) or any(
                ord(c) < 32 or ord(c) == 127 for c in str(val)
            ):
                raise ValueError("Invalid Action request header")
            headers[key] = str(val)
    return path, query, body, headers


def _full_url(server: str, path: str) -> str:
    # Original createURL: domain (OpenAPI servers[0].url) + path, not urljoin.
    base = urlsplit(server)
    if (
        base.scheme not in {"http", "https"}
        or not base.hostname
        or base.username
        or base.password
    ):
        raise ValueError("Invalid Action server URL")
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("Invalid Action path")
    if "?" in path or "#" in path:
        raise ValueError("Action path must not supply URL query or fragment")
    return urlunsplit(
        (base.scheme, base.netloc, base.path.rstrip("/") + path, "", "")
    )


def _authorization(meta: dict, creds: dict) -> dict:
    auth = meta.get("auth") or {}
    kind = auth.get("type", "none")
    if kind == "none":
        return {}
    if kind == "oauth":
        access = creds.get("oauth_access_token")
        if not isinstance(access, str) or not access:
            raise PermissionError("OAuth Action login required")
        return {"Authorization": "Bearer " + access}
    if kind != "service_http" or not creds.get("api_key"):
        raise ValueError("Action credentials unavailable")
    key = creds["api_key"]
    if auth.get("authorization_type") == "basic":
        return {
            "Authorization": "Basic " + base64.b64encode(key.encode()).decode()
        }
    if auth.get("authorization_type") == "bearer":
        return {"Authorization": "Bearer " + key}
    if auth.get("authorization_type") == "custom":
        return {auth["custom_auth_header"]: key}
    raise ValueError("Invalid Action authentication")


def _scrub(text: str, creds: dict) -> str:
    variants = set()
    for secret in creds.values():
        if isinstance(secret, str) and secret:
            variants.update(
                (
                    secret,
                    base64.b64encode(secret.encode()).decode(),
                    quote(secret, safe=""),
                    json.dumps(secret, ensure_ascii=False)[1:-1],
                    json.dumps(secret)[1:-1],
                )
            )
    for variant in sorted(variants, key=len, reverse=True):
        text = text.replace(variant, "[redacted]")
    return text


async def execute_action(
    metadata: dict, creds: dict, operation: dict, arguments: dict
) -> str:
    """Execute exactly one request, with total deadline and no redirect or retry.

    The caller must own the approval/receipt edge; this transport never
    decides whether an external side effect is authorized.
    """
    path, query, body, headers = _request(operation, arguments)
    url = _full_url(operation["server"], path)
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    auth_headers = _authorization(metadata, creds)
    if (
        auth_headers
        and parsed.scheme != "https"
        and not _allowed_local(parsed.hostname or "", port)
    ):
        raise PermissionError(
            "Action credentials require HTTPS or explicit private target"
        )
    # Model-supplied headers cannot shadow credential headers by casing.
    if {key.lower() for key in headers} & {key.lower() for key in auth_headers}:
        raise ValueError("Action request conflicts with authentication header")
    headers.update(auth_headers)
    headers["Accept-Encoding"] = "identity"
    if operation["content_type"]:
        if operation["content_type"] != "application/json":
            raise ValueError("Unsupported Action request content type")
        headers["Content-Type"] = operation["content_type"]
    try:
        async with asyncio.timeout(_TIMEOUT_S):
            async with httpx.AsyncClient(
                transport=ActionHTTPTransport(operation["server"]),
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(_TIMEOUT_S),
            ) as client:
                request = client.build_request(
                    operation["method"].upper(),
                    url,
                    headers=headers,
                    params=query,
                    json=body if operation["method"] != "get" else None,
                )
                if (
                    len(str(request.url).encode())
                    + sum(len(k) + len(v) for k, v in request.headers.raw)
                    + len(request.content)
                    > _MAX_REQUEST
                ):
                    raise ValueError("Action request exceeds size limit")
                response = await client.send(request, stream=True)
                try:
                    if not 200 <= response.status_code < 300:
                        return (
                            "API call failed (HTTP "
                            + str(response.status_code)
                            + ")"
                        )
                    if (
                        response.headers.get(
                            "content-encoding", "identity"
                        ).lower()
                        != "identity"
                    ):
                        return "API call failed (unsupported response encoding)"
                    chunks = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(chunks) + len(chunk) > _MAX_RESPONSE:
                            return "API call failed (response too large)"
                        chunks.extend(chunk)
                    if not chunks:
                        return ""
                    try:
                        value = json.loads(chunks)
                    except ValueError, UnicodeDecodeError, RecursionError:
                        return _scrub(
                            chunks.decode("utf-8", errors="replace"), creds
                        )
                    rendered = (
                        json.dumps(
                            value, ensure_ascii=False, separators=(",", ":")
                        )
                        if isinstance(value, (dict, list))
                        else str(value)
                    )
                    return _scrub(rendered, creds)
                finally:
                    await response.aclose()
    except asyncio.CancelledError:
        raise
    except TimeoutError, httpx.TimeoutException:
        return "API call failed (timeout)"
    except httpx.RequestError:
        return "API call failed (network error)"


async def invoke_action(
    owner: str,
    agent_id: str,
    tool: str,
    arguments: dict,
    database,
    *,
    oauth_token: str | None = None,
    expected_version: int | None = None,
) -> str:
    record = ActionRepository(database).get_for_tool(owner, agent_id, tool)
    if record is None:
        raise PermissionError("Action is not bound to this Agent")
    meta, creds, operation = record
    if (
        expected_version is not None
        and operation["action_version"] != expected_version
    ):
        raise PermissionError("OAuth Action updated while authorizing")
    if meta.get("auth", {}).get("type") == "oauth":
        if not oauth_token:
            raise PermissionError("OAuth Action login required")
        creds = {**creds, "oauth_access_token": oauth_token}
    try:
        return await execute_action(meta, creds, operation, arguments)
    except asyncio.CancelledError:
        raise
    except (PermissionError, ValueError) as exc:
        # Validation messages never echo model arguments or credentials.
        return "API call failed (" + type(exc).__name__ + ")"
    except Exception:
        return "API call failed (execution error)"
