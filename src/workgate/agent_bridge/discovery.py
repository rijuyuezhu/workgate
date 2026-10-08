"""Bounded process-local discovery index for external MCP tools."""

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from .mcp import (
    MAX_SERVER_DESCRIPTOR_BYTES,
    MAX_TOOL_DESCRIPTOR_BYTES,
    MAX_TOOLS_PER_SERVER,
)

# Entries are complete, already redacted tool rows from the owning Control/executor.
_MAX_SCOPES = 16
_MAX_SERVERS = 32
_MAX_SCOPE_BYTES = 2 * 1024 * 1024
_CACHE_SECONDS = 60


class McpDiscovery:
    """Keep a small, expiring catalog; never retain MCP processes or credentials."""

    def __init__(
        self,
        fetch: Callable[
            [str | None, str | None], Awaitable[list[dict[str, Any]]]
        ],
    ) -> None:
        self._fetch = fetch
        self._cache: OrderedDict[
            tuple[str | None, str | None], tuple[float, list[dict[str, Any]]]
        ] = OrderedDict()
        self._pending: dict[
            tuple[str | None, str | None], asyncio.Task[list[dict[str, Any]]]
        ] = {}

    def invalidate(self, session_id: str | None = None) -> None:
        for key in tuple(self._cache):
            if key[0] == session_id:
                del self._cache[key]
        # Old operations may finish, but must not repopulate an invalidated scope.
        for key in tuple(self._pending):
            if key[0] == session_id:
                del self._pending[key]

    def invalidate_all(self) -> None:
        """Discard cached metadata across sessions after a manifest mutation."""
        self._cache.clear()
        self._pending.clear()

    async def _rows(
        self,
        session_id: str | None,
        server: str | None,
        *,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        key = (session_id, server)
        now = time.monotonic()
        if not refresh:
            # A fresh full catalog also satisfies a targeted inspection.
            for candidate in (key, (session_id, None)) if server else (key,):
                cached = self._cache.get(candidate)
                if cached is not None and now < cached[0]:
                    self._cache.move_to_end(candidate)
                    return cached[1]

        pending = self._pending.get(key)
        if pending is None or refresh:
            pending = asyncio.create_task(self._load(key))
            self._pending[key] = pending
        return await asyncio.shield(pending)

    async def _load(
        self, key: tuple[str | None, str | None]
    ) -> list[dict[str, Any]]:
        try:
            rows = await self._fetch(*key)
            self._store(
                key,
                rows,
                cache=self._pending.get(key) is asyncio.current_task(),
            )
            return rows
        finally:
            if self._pending.get(key) is asyncio.current_task():
                del self._pending[key]

    def _store(
        self,
        key: tuple[str | None, str | None],
        rows: list[dict[str, Any]],
        *,
        cache: bool,
    ) -> None:
        counts: dict[str, tuple[int, int]] = {}
        total_bytes = 0
        for row in rows:
            server = str(row["server"])
            size = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
            if size > MAX_TOOL_DESCRIPTOR_BYTES:
                raise ValueError("MCP tool descriptor exceeds size limit")
            count, previous_bytes = counts.get(server, (0, 0))
            if (
                count >= MAX_TOOLS_PER_SERVER
                or previous_bytes + size > MAX_SERVER_DESCRIPTOR_BYTES
            ):
                raise ValueError("MCP server tool catalog exceeds bounds")
            counts[server] = (count + 1, previous_bytes + size)
            total_bytes += size
            if len(counts) > _MAX_SERVERS or total_bytes > _MAX_SCOPE_BYTES:
                raise ValueError("MCP discovery catalog exceeds bounds")

        if not cache:
            return
        self._cache[key] = (time.monotonic() + _CACHE_SECONDS, rows)
        self._cache.move_to_end(key)
        while len(self._cache) > _MAX_SCOPES:
            self._cache.popitem(last=False)

    async def search(
        self,
        query: str,
        *,
        session_id: str | None = None,
        server: str | None = None,
        limit: int = 20,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        tokens = query.casefold().split()
        rows = await self._rows(session_id, server, refresh=refresh)
        matches: list[tuple[int, dict[str, str]]] = []
        for row in rows:
            name, tool = str(row["server"]), str(row["tool"])
            if server is not None and name != server:
                continue
            description = str(row.get("description") or "")
            haystack = f"{name} {tool} {description}".casefold()
            if not all(token in haystack for token in tokens):
                continue
            identifier = f"{name}:{tool}".casefold()
            score = sum(3 if token in identifier else 1 for token in tokens)
            matches.append(
                (
                    score,
                    {
                        "server": name,
                        "tool": tool,
                        "description": description[:512],
                    },
                )
            )
        matches.sort(
            key=lambda item: (-item[0], item[1]["server"], item[1]["tool"])
        )
        return {
            "tools": [row for _, row in matches[:limit]],
            "total_matches": len(matches),
        }

    async def inspect(
        self, server: str, tool: str, *, session_id: str | None = None
    ) -> dict[str, Any]:
        rows = await self._rows(session_id, server)
        for row in rows:
            if row["server"] == server and row["tool"] == tool:
                return dict(row)
        raise ValueError(
            f"Unknown agent MCP tool: {server}:{tool}; search again"
        )
