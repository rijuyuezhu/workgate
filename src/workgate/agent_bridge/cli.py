"""Command-line credential and OAuth administration for Agent Bridge MCP servers."""

import argparse
import asyncio
import json
import sys
import webbrowser
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    extract_resource_metadata_from_www_auth,
)
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

from ..config.cli import register_config_and_setting_args, settings_from_args
from ..config.settings import Settings
from .auth import (
    build_stored_oauth_provider,
    credential_store_key,
    oauth_status,
)
from .auth_store import AgentAuthStore
from .mcp import AgentMcpClientManager
from .models import AgentMcpServerConfig
from .state import load_agent_manifest

_MAX_CALLBACK_REQUEST_BYTES = 16_384
_MAX_STDIN_SECRET_BYTES = 65_536


def register_mcp_cli(subparsers: Any) -> argparse.ArgumentParser:
    """Register public Agent Bridge auth and secret administration commands."""
    mcp_parser = subparsers.add_parser(
        "mcp",
        help="Manage Agent Bridge MCP credentials and OAuth authorization",
        description="Manage Agent Bridge MCP credentials and OAuth authorization.",
    )
    register_config_and_setting_args(mcp_parser)
    mcp_subparsers = mcp_parser.add_subparsers(
        dest="mcp_command", required=True
    )

    auth_parser = mcp_subparsers.add_parser(
        "auth", help="Authorize, inspect, or log out an OAuth MCP server."
    )
    auth_parser.add_argument("server")
    auth_actions = auth_parser.add_mutually_exclusive_group()
    auth_actions.add_argument("--status", action="store_true")
    auth_actions.add_argument("--logout", action="store_true")
    auth_actions.add_argument(
        "--no-open",
        action="store_true",
        help="Print the authorization URL without opening a browser.",
    )
    auth_parser.set_defaults(handler=run_mcp_cli_from_args)

    secret_parser = mcp_subparsers.add_parser(
        "secret",
        help="Manage private values referenced by Agent Bridge manifests.",
    )
    secret_subparsers = secret_parser.add_subparsers(
        dest="secret_command", required=True
    )
    set_parser = secret_subparsers.add_parser("set")
    set_parser.add_argument("server")
    set_parser.add_argument("name")
    set_parser.add_argument("--stdin", action="store_true", required=True)
    set_parser.set_defaults(handler=run_mcp_cli_from_args)

    list_parser = secret_subparsers.add_parser("list")
    list_parser.add_argument("server", nargs="?")
    list_parser.set_defaults(handler=run_mcp_cli_from_args)

    delete_parser = secret_subparsers.add_parser("delete")
    delete_parser.add_argument("server")
    delete_parser.add_argument("name")
    delete_parser.set_defaults(handler=run_mcp_cli_from_args)
    return mcp_parser


def _settings_from_args(args: argparse.Namespace) -> Settings:
    return settings_from_args(args)


def _configured_server(
    settings: Settings, server_name: str
) -> AgentMcpServerConfig:
    loaded = load_agent_manifest(settings.agent_config_dir)
    if loaded.status != "loaded":
        detail = "; ".join(loaded.errors) or loaded.status
        raise ValueError(f"Agent Bridge manifest is unavailable: {detail}")
    try:
        return loaded.data.mcp_servers[server_name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown Agent Bridge MCP server: {server_name}"
        ) from exc


def _credential_key_for_cleanup(
    settings: Settings,
    store: AgentAuthStore,
    identifier: str,
) -> str:
    """Resolve a live label while preserving direct detached-id cleanup."""
    if store.list_secrets(identifier):
        return identifier
    loaded = load_agent_manifest(settings.agent_config_dir)
    if loaded.status == "loaded":
        server = loaded.data.mcp_servers.get(identifier)
        if server is not None:
            return credential_store_key(identifier, server)
    return identifier


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _read_secret_stdin() -> str:
    if sys.stdin.isatty():
        raise ValueError(
            "refusing to read a secret from an interactive terminal"
        )
    payload = sys.stdin.buffer.read(_MAX_STDIN_SECRET_BYTES + 1)
    if len(payload) > _MAX_STDIN_SECRET_BYTES:
        raise ValueError(
            f"secret input exceeds {_MAX_STDIN_SECRET_BYTES} bytes"
        )
    try:
        value = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("secret input must be UTF-8 text") from exc
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if not value:
        raise ValueError("secret input must not be empty")
    return value


class LoopbackOAuthCallback:
    """One-shot owner-local HTTP callback used by the interactive OAuth CLI."""

    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._result: asyncio.Future[tuple[str, str | None]] | None = None
        self.redirect_uri = ""

    async def __aenter__(self) -> LoopbackOAuthCallback:
        loop = asyncio.get_running_loop()
        self._result = loop.create_future()
        self._server = await asyncio.start_server(
            self._handle_connection,
            "127.0.0.1",
            0,
            limit=_MAX_CALLBACK_REQUEST_BYTES,
        )
        socket = self._server.sockets[0]
        port = int(socket.getsockname()[1])
        self.redirect_uri = f"http://127.0.0.1:{port}/callback"
        return self

    async def __aexit__(self, *_args: Any) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def wait(self) -> tuple[str, str | None]:
        """Wait for the single valid authorization callback result."""
        if self._result is None:
            raise RuntimeError("OAuth callback listener has not started")
        return await self._result

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        status = "400 Bad Request"
        body = "Authorization callback was invalid."
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            if (
                not request_line
                or len(request_line) > _MAX_CALLBACK_REQUEST_BYTES
            ):
                raise ValueError("invalid callback request")
            consumed = len(request_line)
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                consumed += len(line)
                if consumed > _MAX_CALLBACK_REQUEST_BYTES:
                    raise ValueError("callback request is too large")
                if line in {b"\r\n", b"\n", b""}:
                    break
            method, target, _version = (
                request_line.decode("ascii").strip().split(" ", 2)
            )
            parsed = urlsplit(target)
            if method != "GET" or parsed.path != "/callback":
                raise ValueError("unexpected callback path")
            query = parse_qs(parsed.query)
            if query.get("error"):
                description = query.get("error_description", query["error"])[0]
                raise ValueError(f"OAuth authorization failed: {description}")
            code = query.get("code", [""])[0]
            state = query.get("state", [None])[0]
            if not code:
                raise ValueError("OAuth callback omitted authorization code")
            if self._result is not None and not self._result.done():
                self._result.set_result((code, state))
            status = "200 OK"
            body = "Authorization complete. You may close this window."
        except Exception as exc:
            if self._result is not None and not self._result.done():
                self._result.set_exception(exc)
        finally:
            encoded = body.encode("utf-8")
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {len(encoded)}\r\nConnection: close\r\n\r\n".encode(
                    "ascii"
                )
                + encoded
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()


async def authorize_server(
    settings: Settings,
    server_name: str,
    server: AgentMcpServerConfig,
    *,
    no_open: bool,
) -> dict[str, Any]:
    """Complete interactive SDK OAuth and verify it with tools/list."""
    if server.auth.mode != "oauth":
        raise ValueError(
            f"Agent Bridge server {server_name} is not configured for OAuth"
        )
    store = AgentAuthStore(settings.agent_auth_dir)
    async with LoopbackOAuthCallback() as callback:

        async def redirect_handler(url: str) -> None:
            print(f"Authorize {server_name}: {url}")
            if not no_open:
                opened = await asyncio.to_thread(webbrowser.open, url)
                if not opened:
                    print(
                        "Browser did not open; use the URL above.",
                        file=sys.stderr,
                    )

        provider = build_stored_oauth_provider(
            store,
            server_name,
            server,
            redirect_uri=callback.redirect_uri,
            redirect_handler=redirect_handler,
            callback_handler=callback.wait,
            timeout=max(30, settings.agent_mcp_call_timeout_s),
        )
        manager = AgentMcpClientManager(
            settings.agent_mcp_call_timeout_s,
            store,
            oauth_provider_factory=lambda _name, _server: provider,
        )
        tools = await manager.list_tools(server_name, server)
    result = oauth_status(store, server_name, server)
    result["server"] = server_name
    result["tool_count"] = len(tools)
    return result


@dataclass(frozen=True)
class RevocationResult:
    """Remote OAuth revocation outcome reported by logout."""

    status: str
    """Stable result category: revoked, unsupported, failed, or not_authorized."""
    detail: str | None = None
    """Optional bounded explanation that never includes token values."""


async def _discover_oauth_metadata(
    client: httpx.AsyncClient, server_url: str
) -> OAuthMetadata | None:
    initial = await client.get(server_url)
    hint = extract_resource_metadata_from_www_auth(initial)
    protected: ProtectedResourceMetadata | None = None
    for url in build_protected_resource_metadata_discovery_urls(
        hint, server_url
    ):
        response = await client.get(url)
        if response.status_code == 200:
            try:
                protected = ProtectedResourceMetadata.model_validate_json(
                    response.content
                )
            except Exception:
                continue
            break
    auth_server = (
        str(protected.authorization_servers[0])
        if protected and protected.authorization_servers
        else None
    )
    for url in build_oauth_authorization_server_metadata_discovery_urls(
        auth_server, server_url
    ):
        response = await client.get(url)
        if response.status_code == 200:
            try:
                return OAuthMetadata.model_validate_json(response.content)
            except Exception:
                continue
    return None


async def revoke_stored_oauth(
    store: AgentAuthStore, server_name: str, server: AgentMcpServerConfig
) -> RevocationResult:
    """Attempt standards-based remote token revocation before local logout."""
    credential_key = credential_store_key(server_name, server)
    tokens = store.get_tokens(credential_key)
    client_info = store.get_client_info(credential_key)
    if tokens is None or not server.url:
        return RevocationResult("not_authorized")
    token = tokens.refresh_token or tokens.access_token
    hint = "refresh_token" if tokens.refresh_token else "access_token"
    try:
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=True
        ) as client:
            metadata = await _discover_oauth_metadata(client, server.url)
            if metadata is None or metadata.revocation_endpoint is None:
                return RevocationResult(
                    "unsupported", "no revocation endpoint advertised"
                )
            data: dict[str, str] = {"token": token, "token_type_hint": hint}
            auth: httpx.Auth | None = None
            if client_info is not None and client_info.client_id:
                data["client_id"] = client_info.client_id
                method = client_info.token_endpoint_auth_method or "none"
                if (
                    client_info.client_secret
                    and method == "client_secret_basic"
                ):
                    auth = httpx.BasicAuth(
                        client_info.client_id, client_info.client_secret
                    )
                elif (
                    client_info.client_secret and method == "client_secret_post"
                ):
                    data["client_secret"] = client_info.client_secret
            if auth is None:
                response = await client.post(
                    str(metadata.revocation_endpoint), data=data
                )
            else:
                response = await client.post(
                    str(metadata.revocation_endpoint), data=data, auth=auth
                )
            if response.status_code not in {200, 204}:
                return RevocationResult(
                    "failed",
                    f"revocation endpoint returned HTTP {response.status_code}",
                )
            return RevocationResult("revoked")
    except Exception as exc:
        return RevocationResult("failed", f"{type(exc).__name__}: {exc}")


def run_mcp_cli_from_args(args: argparse.Namespace) -> None:
    """Dispatch Agent Bridge credential CLI operations with explicit safe failures."""
    try:
        settings = _settings_from_args(args)
        store = AgentAuthStore(settings.agent_auth_dir)
        if args.mcp_command == "secret":
            if args.secret_command not in {"set", "list", "delete"}:
                raise ValueError(
                    f"unsupported secret command: {args.secret_command}"
                )
            match args.secret_command:
                case "set":
                    server = _configured_server(settings, args.server)
                    credential_key = credential_store_key(args.server, server)
                    store.set_secret(
                        credential_key, args.name, _read_secret_stdin()
                    )
                    _print_json(
                        {
                            "server": args.server,
                            "name": args.name,
                            "stored": True,
                        }
                    )
                    return
                case "list":
                    if args.server is None:
                        _print_json({"secrets": store.list_secrets()})
                        return
                    credential_key = _credential_key_for_cleanup(
                        settings, store, args.server
                    )
                    stored = store.list_secrets(credential_key).get(
                        credential_key, []
                    )
                    _print_json(
                        {"secrets": ({args.server: stored} if stored else {})}
                    )
                    return
                case "delete":
                    credential_key = _credential_key_for_cleanup(
                        settings, store, args.server
                    )
                    deleted = store.delete_secret(credential_key, args.name)
                    _print_json(
                        {
                            "server": args.server,
                            "name": args.name,
                            "deleted": deleted,
                        }
                    )
                    return

        server = _configured_server(settings, args.server)
        if server.auth.mode != "oauth":
            raise ValueError(
                f"Agent Bridge server {args.server} is not configured for OAuth"
            )
        if args.status:
            status = oauth_status(store, args.server, server)
            status["server"] = args.server
            _print_json(status)
            return
        if args.logout:
            revocation = asyncio.run(
                revoke_stored_oauth(store, args.server, server)
            )
            cleared = store.clear_oauth(
                credential_store_key(args.server, server)
            )
            _print_json(
                {
                    "server": args.server,
                    "local_credentials_cleared": cleared,
                    "remote_revocation": revocation.status,
                    "detail": revocation.detail,
                }
            )
            if revocation.status == "failed":
                raise SystemExit(1)
            return
        _print_json(
            asyncio.run(
                authorize_server(
                    settings, args.server, server, no_open=args.no_open
                )
            )
        )
    except SystemExit:
        raise
    except Exception as exc:
        print(f"workgate mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
