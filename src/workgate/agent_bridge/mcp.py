"""Normalize upstream MCP protocol objects and manage client sessions for configured agent bridge servers."""

import asyncio
import threading
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import PaginatedRequestParams

from ..utils.serialization import to_jsonable
from .auth import (
    OAuthProviderFactory,
    build_stored_oauth_provider,
    oauth_status,
    resolve_config_mapping,
)
from .auth_store import AgentAuthStore
from .models import AgentMcpServerConfig


@dataclass(frozen=True)
class _CredentialRedactionBaseline:
    """Long-lived credential history anchor for one configured MCP server."""

    revision: int


@dataclass(frozen=True)
class AgentMcpTool:
    """Normalized description of an upstream MCP tool exposed through the bridge."""

    name: str
    """Upstream MCP tool name."""
    description: str
    """Human-readable tool description advertised by the upstream server."""
    input_schema: dict[str, Any]
    """JSON schema describing the upstream tool input payload."""


@dataclass(frozen=True)
class _StdioWorkerConfig:
    """Resolved process configuration that determines stdio worker identity."""

    command: str
    args: tuple[str, ...]
    env: tuple[tuple[str, str], ...]

    def server_parameters(self) -> StdioServerParameters:
        """Build MCP SDK process parameters without exposing resolved values publicly."""
        return StdioServerParameters(
            command=self.command,
            args=list(self.args),
            env=dict(self.env) or None,
        )


def _value(source: Any, name: str, default: Any = None) -> Any:
    """Read an MCP protocol field from either a mapping or SDK object."""
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def normalize_mcp_tool(tool: Any) -> AgentMcpTool:
    """Normalize SDK-specific MCP tool objects into a stable serializable shape."""
    input_schema = _value(tool, "inputSchema")
    if input_schema is None:
        input_schema = _value(tool, "input_schema", {})

    return AgentMcpTool(
        name=str(_value(tool, "name", "")),
        description=str(_value(tool, "description", "") or ""),
        input_schema=input_schema,
    )


def _normalize_content_item(item: Any) -> Any:
    """Convert MCP content blocks to JSON-serializable dictionaries while preserving unknown fields."""
    if _value(item, "type") == "text":
        return {"type": "text", "text": _value(item, "text", "")}
    jsonable_item = to_jsonable(item)
    if isinstance(jsonable_item, dict):
        return jsonable_item
    return {"type": "repr", "repr": repr(item)}


def normalize_tool_result(result: Any) -> dict[str, Any]:
    """Convert an MCP tool result into a stable payload with content blocks and error state."""
    structured_content = _value(result, "structuredContent")
    if structured_content is None:
        structured_content = _value(result, "structured_content")
    structured_content = to_jsonable(structured_content)

    return {
        "is_error": bool(
            _value(result, "isError", False)
            or _value(result, "is_error", False)
        ),
        "content": [
            _normalize_content_item(item)
            for item in _value(result, "content", [])
        ],
        "structured_content": structured_content,
    }


async def _list_session_tools(session: ClientSession) -> list[AgentMcpTool]:
    """Page through one initialized MCP session's tool list."""
    tools: list[AgentMcpTool] = []
    cursor: str | None = None
    while True:
        result = await session.list_tools(
            params=PaginatedRequestParams(cursor=cursor)
        )
        tools.extend(
            normalize_mcp_tool(tool) for tool in _value(result, "tools", result)
        )
        cursor = getattr(result, "nextCursor", None)
        if not cursor:
            return tools


class _PersistentStdioWorker:
    """Own one stdio MCP process and ClientSession on a dedicated event-loop thread."""

    def __init__(self, name: str, config: _StdioWorkerConfig) -> None:
        self.name = name
        self.config = config
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"agent-mcp-stdio-{name}",
            daemon=True,
        )
        self._start_lock = threading.Lock()
        self._started = False
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._close_requested = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main_task: asyncio.Task[Any] | None = None
        self._stop_event: asyncio.Event | None = None
        self._operation_lock: asyncio.Lock | None = None
        self._session: ClientSession | None = None
        self._startup_error: BaseException | None = None
        self._phase_lock = threading.Lock()
        self._phase = "new"

    def start(self) -> None:
        """Start the worker thread exactly once."""
        with self._start_lock:
            if self._started:
                return
            self._started = True
            self._thread.start()

    @property
    def usable(self) -> bool:
        """Return whether this worker can still accept operations."""
        if self._close_requested.is_set() or self._closed.is_set():
            return False
        return not self._started or self._thread.is_alive()

    def _thread_main(self) -> None:
        """Run the full transport lifecycle inside one stable asyncio loop."""
        try:
            asyncio.run(self._run())
        except BaseException as exc:
            if not self._ready.is_set():
                self._startup_error = exc
        finally:
            self._set_phase("closed")
            self._ready.set()
            self._closed.set()

    def _set_phase(self, phase: str) -> None:
        """Publish one coarse worker lifecycle phase across threads."""
        with self._phase_lock:
            self._phase = phase

    def _phase_value(self) -> str:
        with self._phase_lock:
            return self._phase

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._main_task = asyncio.current_task()
        self._stop_event = asyncio.Event()
        if self._close_requested.is_set():
            self._set_phase("retiring")
            return
        self._set_phase("starting")

        try:
            async with (
                stdio_client(self.config.server_parameters()) as (
                    read_stream,
                    write_stream,
                ),
                ClientSession(read_stream, write_stream) as session,
            ):
                try:
                    await session.initialize()
                except BaseException:
                    self._set_phase("retiring")
                    raise
                if self._close_requested.is_set():
                    self._set_phase("retiring")
                    return
                self._session = session
                self._operation_lock = asyncio.Lock()
                self._set_phase("running")
                self._ready.set()
                try:
                    await self._stop_event.wait()
                finally:
                    self._set_phase("retiring")
        except BaseException as exc:
            if not self._ready.is_set():
                self._set_phase("retiring")
                self._startup_error = exc
                self._ready.set()
            raise
        finally:
            self._session = None
            self._operation_lock = None

    async def _wait_ready(self) -> None:
        self.start()
        while not self._ready.is_set():
            await asyncio.sleep(0.01)
        if self._startup_error is not None:
            if isinstance(self._startup_error, Exception):
                raise self._startup_error
            raise RuntimeError(
                f"stdio MCP worker {self.name} stopped during startup"
            ) from self._startup_error
        if self._session is None or self._loop is None:
            raise RuntimeError(f"stdio MCP worker {self.name} is not running")

    async def _run_operation(self, coroutine: Any) -> Any:
        await self._wait_ready()
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError(
                f"stdio MCP worker {self.name} event loop stopped"
            )
        future = asyncio.run_coroutine_threadsafe(coroutine(), loop)
        return await asyncio.wrap_future(future)

    async def _list_tools(self) -> list[AgentMcpTool]:
        if self._session is None or self._operation_lock is None:
            raise RuntimeError(f"stdio MCP worker {self.name} is not ready")
        async with self._operation_lock:
            return await _list_session_tools(self._session)

    async def list_tools(self) -> list[AgentMcpTool]:
        """List tools through the persistent session from any caller event loop."""
        return await self._run_operation(self._list_tools)

    async def _call_tool(
        self, tool: str, args: dict[str, Any] | None
    ) -> dict[str, Any]:
        if self._session is None or self._operation_lock is None:
            raise RuntimeError(f"stdio MCP worker {self.name} is not ready")
        async with self._operation_lock:
            return normalize_tool_result(
                await self._session.call_tool(tool, args)
            )

    async def call_tool(
        self, tool: str, args: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Invoke one tool through the persistent session from any caller event loop."""

        async def operation() -> dict[str, Any]:
            return await self._call_tool(tool, args)

        return await self._run_operation(operation)

    async def _request_stop(self) -> None:
        stop_event = self._stop_event
        if stop_event is None:
            return
        operation_lock = self._operation_lock
        if operation_lock is None:
            stop_event.set()
            return
        async with operation_lock:
            stop_event.set()

    def _request_close_on_loop(self) -> None:
        """Cancel only startup; use the graceful stop path after initialize."""
        phase = self._phase_value()
        if phase == "starting":
            self._set_phase("retiring")
            task = self._main_task
            if task is not None:
                task.cancel()
            return
        if phase == "running":
            asyncio.create_task(self._request_stop())

    def request_close(self) -> None:
        """Ask the worker to stop without waiting for process teardown."""
        self._close_requested.set()
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(self._request_close_on_loop)

    def close(self, timeout_s: float = 6) -> None:
        """Stop the worker and return only after its transport thread retired."""
        self.request_close()
        if not self._started:
            return
        self._thread.join(timeout=max(0, timeout_s))
        if self._thread.is_alive():
            raise RuntimeError(
                f"stdio MCP worker {self.name} did not retire within {timeout_s:g}s"
            )


class AgentMcpClientManager:
    """Manage persistent stdio MCP sessions and short-lived HTTP/SSE sessions."""

    def __init__(
        self,
        call_timeout_s: float = 60,
        auth_store: AgentAuthStore | None = None,
        oauth_provider_factory: OAuthProviderFactory | None = None,
        *,
        allow_stdio: bool = True,
    ) -> None:
        self.call_timeout_s = call_timeout_s
        self.auth_store = auth_store
        self.oauth_provider_factory = oauth_provider_factory
        self.allow_stdio = allow_stdio
        self._stdio_workers: dict[
            str, tuple[_StdioWorkerConfig, _PersistentStdioWorker]
        ] = {}
        self._stdio_workers_lock = threading.RLock()
        self._stdio_lifecycle_locks: dict[str, threading.RLock] = {}
        self._stdio_lifecycle_locks_lock = threading.Lock()
        self._credential_redaction_baselines: dict[
            str, _CredentialRedactionBaseline
        ] = {}
        self._credential_redaction_lock = threading.RLock()

    def resolved_maps(
        self, name: str, server: AgentMcpServerConfig
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Resolve transport env/headers immediately before opening a connection."""
        return (
            resolve_config_mapping(self.auth_store, name, server.env),
            resolve_config_mapping(self.auth_store, name, server.headers),
        )

    def redaction_maps(
        self, name: str, server: AgentMcpServerConfig
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Resolve transport and owner-held credential values solely for redaction."""
        env, headers = self.resolved_maps(name, server)
        if self.auth_store is not None and server.auth.mode == "oauth":
            env = {**env, **self.auth_store.oauth_redaction_values(name)}
        return env, headers

    def redaction_cursor(
        self, name: str, server: AgentMcpServerConfig
    ) -> int | None:
        """Return a durable-history baseline for one authenticated MCP server."""
        if self.auth_store is None:
            return None
        with self._credential_redaction_lock:
            baseline = self._credential_redaction_baselines.get(name)
            if baseline is not None:
                return baseline.revision
            if server.auth.mode not in {"oauth", "secret"}:
                return None

            revision = self.auth_store.redaction_cursor(name)
            if not self.auth_status(name, server).get("authorized", False):
                # No authenticated upstream operation can use these credentials yet.
                return revision

            # A fresh manager must reconstruct the complete retained history instead
            # of silently rebasing at the current revision after a process restart.
            self.auth_store.credential_redaction_values_since(name, 0)
            self._credential_redaction_baselines[name] = (
                _CredentialRedactionBaseline(revision=0)
            )
            return 0

    def redaction_maps_since(
        self,
        name: str,
        server: AgentMcpServerConfig,
        cursor: int | None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Resolve current and durable retained credentials since the safe baseline."""
        env, headers = self.redaction_maps(name, server)
        if cursor is None or self.auth_store is None:
            return env, headers

        with self._credential_redaction_lock:
            baseline = self._credential_redaction_baselines.get(name)
        effective_cursor = (
            min(cursor, baseline.revision) if baseline is not None else cursor
        )
        observed = self.auth_store.credential_redaction_values_since(
            name, effective_cursor
        )
        return {**env, **observed}, headers

    def auth_status(
        self, name: str, server: AgentMcpServerConfig
    ) -> dict[str, Any]:
        """Return public authorization status for registry payloads."""
        return oauth_status(self.auth_store, name, server)

    def _oauth_auth(
        self, name: str, server: AgentMcpServerConfig
    ) -> httpx.Auth | None:
        if server.auth.mode != "oauth":
            return None
        if self.oauth_provider_factory is not None:
            return self.oauth_provider_factory(name, server)
        if self.auth_store is None:
            raise ValueError(
                f"OAuth authorization required for {name}; run workgate mcp auth {name}"
            )
        status = self.auth_status(name, server)
        if not status["authorized"]:
            raise ValueError(
                f"OAuth authorization required for {name}; run workgate mcp auth {name}"
            )
        return build_stored_oauth_provider(self.auth_store, name, server)

    def _stdio_worker_config(
        self, name: str, server: AgentMcpServerConfig
    ) -> _StdioWorkerConfig:
        if not server.command:
            raise ValueError("stdio MCP server requires command")
        env, _headers = self.resolved_maps(name, server)
        return _StdioWorkerConfig(
            command=server.command,
            args=tuple(server.args),
            env=tuple(sorted(env.items())),
        )

    def _stdio_lifecycle_lock(self, name: str) -> threading.RLock:
        """Return the per-server barrier that serializes retirement and replacement."""
        with self._stdio_lifecycle_locks_lock:
            return self._stdio_lifecycle_locks.setdefault(
                name, threading.RLock()
            )

    def _retire_stdio_worker(
        self, name: str, worker: _PersistentStdioWorker
    ) -> bool:
        """Retire one current worker before making its name available again."""
        with self._stdio_lifecycle_lock(name):
            with self._stdio_workers_lock:
                current = self._stdio_workers.get(name)
                if current is None or current[1] is not worker:
                    return False
            worker.close()
            with self._stdio_workers_lock:
                current = self._stdio_workers.get(name)
                if current is not None and current[1] is worker:
                    self._stdio_workers.pop(name, None)
            return True

    def _stdio_worker(
        self, name: str, server: AgentMcpServerConfig
    ) -> _PersistentStdioWorker:
        config = self._stdio_worker_config(name, server)
        with self._stdio_lifecycle_lock(name):
            with self._stdio_workers_lock:
                current = self._stdio_workers.get(name)
                if (
                    current is not None
                    and current[0] == config
                    and current[1].usable
                ):
                    return current[1]
            if current is not None:
                current[1].close()
                with self._stdio_workers_lock:
                    if self._stdio_workers.get(name) == current:
                        self._stdio_workers.pop(name, None)
            worker = _PersistentStdioWorker(name, config)
            worker.start()
            with self._stdio_workers_lock:
                self._stdio_workers[name] = (config, worker)
            return worker

    def _discard_stdio_worker(
        self, name: str, worker: _PersistentStdioWorker
    ) -> None:
        self._retire_stdio_worker(name, worker)

    def retain_stdio_servers(
        self, servers: Mapping[str, AgentMcpServerConfig]
    ) -> None:
        """Stop persistent workers for stdio servers removed or disabled by reload."""
        retained_names = {
            name
            for name, server in servers.items()
            if server.enabled and server.type == "stdio"
        }
        with self._stdio_workers_lock:
            stale = [
                (name, worker)
                for name, (_config, worker) in self._stdio_workers.items()
                if name not in retained_names
            ]
        for name, worker in stale:
            self._retire_stdio_worker(name, worker)

    def close(self) -> None:
        """Close all persistent stdio workers owned by this manager."""
        with self._stdio_workers_lock:
            workers = [
                (name, worker)
                for name, (_config, worker) in self._stdio_workers.items()
            ]
        errors: list[Exception] = []
        for name, worker in workers:
            try:
                self._retire_stdio_worker(name, worker)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("failed to retire stdio MCP workers", errors)

    @asynccontextmanager
    async def _session(
        self, name: str, server: AgentMcpServerConfig
    ) -> AsyncGenerator[ClientSession]:
        """Open and initialize the transport-specific MCP client session for one configured server."""
        env, headers = self.resolved_maps(name, server)
        auth = self._oauth_auth(name, server)
        match server.type:
            case "stdio":
                raise RuntimeError(
                    "stdio MCP sessions are managed by the persistent worker"
                )
            case "http":
                if not server.url:
                    raise ValueError("http MCP server requires url")
                async with (
                    httpx.AsyncClient(
                        headers=headers or None, auth=auth
                    ) as client,
                    streamable_http_client(server.url, http_client=client) as (
                        read_stream,
                        write_stream,
                        _get_session_id,
                    ),
                    ClientSession(read_stream, write_stream) as session,
                ):
                    await session.initialize()
                    yield session
            case "sse":
                if not server.url:
                    raise ValueError("sse MCP server requires url")
                async with (
                    sse_client(
                        server.url, headers=headers or None, auth=auth
                    ) as (
                        read_stream,
                        write_stream,
                    ),
                    ClientSession(read_stream, write_stream) as session,
                ):
                    await session.initialize()
                    yield session
            case _:
                raise ValueError(
                    f"unsupported MCP server type for {name}: {server.type}"
                )

    async def list_tools(
        self, name: str, server: AgentMcpServerConfig
    ) -> list[AgentMcpTool]:
        """Page through an upstream server's tool list within the configured call timeout."""

        if server.type == "stdio":
            if not self.allow_stdio:
                raise ValueError("stdio MCP servers are executor-owned")
            worker = self._stdio_worker(name, server)
            try:
                return await asyncio.wait_for(
                    worker.list_tools(), timeout=self.call_timeout_s
                )
            except BaseException:
                self._discard_stdio_worker(name, worker)
                raise

        async def _list_tools() -> list[AgentMcpTool]:
            async with self._session(name, server) as session:
                return await _list_session_tools(session)

        return await asyncio.wait_for(
            _list_tools(), timeout=self.call_timeout_s
        )

    async def call_tool(
        self,
        name: str,
        server: AgentMcpServerConfig,
        tool: str,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invoke an upstream MCP tool and normalize its protocol result for workgate responses."""

        if server.type == "stdio":
            if not self.allow_stdio:
                raise ValueError("stdio MCP servers are executor-owned")
            worker = self._stdio_worker(name, server)
            try:
                return await asyncio.wait_for(
                    worker.call_tool(tool, args), timeout=self.call_timeout_s
                )
            except BaseException:
                self._discard_stdio_worker(name, worker)
                raise

        async def _call_tool() -> dict[str, Any]:
            async with self._session(name, server) as session:
                return normalize_tool_result(
                    await session.call_tool(tool, args)
                )

        return await asyncio.wait_for(_call_tool(), timeout=self.call_timeout_s)
