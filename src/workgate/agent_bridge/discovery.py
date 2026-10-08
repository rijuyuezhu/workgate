"""Bounded process-local discovery index for external MCP tools."""

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
        self, fetch: Callable[[str | None], Awaitable[list[dict[str, Any]]]]
    ) -> None:
        self._fetch = fetch
        self._cache: OrderedDict[
            str | None, tuple[float, list[dict[str, Any]]]
        ] = OrderedDict()

    def invalidate(self, session_id: str | None = None) -> None:
        self._cache.pop(session_id, None)

    async def _rows(
        self, session_id: str | None, *, refresh: bool = False
    ) -> list[dict[str, Any]]:
        now = time.monotonic()
        cached = self._cache.get(session_id)
        if not refresh and cached is not None and now < cached[0]:
            self._cache.move_to_end(session_id)
            return cached[1]

        rows = await self._fetch(session_id)
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

        self._cache[session_id] = (now + _CACHE_SECONDS, rows)
        self._cache.move_to_end(session_id)
        while len(self._cache) > _MAX_SCOPES:
            self._cache.popitem(last=False)
        return rows

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
        rows = await self._rows(session_id, refresh=refresh)
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
        rows = await self._rows(session_id)
        for row in rows:
            if row["server"] == server and row["tool"] == tool:
                return dict(row)
        raise ValueError(
            f"Unknown agent MCP tool: {server}:{tool}; search again"
        )
