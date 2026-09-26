"""Synchronous StateStore adapter for SQLite-backed stateful hosting actors."""

import json
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from ..persistence import StateLayout

_DEFAULT_LOGICAL_ROOT = Path("__workgate_hosted_state__")


class SqlStorageCursor(Protocol):
    """Small cursor surface exposed by Cloudflare Durable Object SQL."""

    def toArray(self) -> Any:
        """Return all rows synchronously."""
        ...


class SqlStorage(Protocol):
    """Dependency-light subset of SQLite-backed Durable Object SQL storage."""

    def exec(self, query: str, *bindings: object) -> SqlStorageCursor:
        """Execute one synchronous SQL statement with positional bindings."""
        ...


def _cursor_rows(cursor: SqlStorageCursor) -> list[Any]:
    return list(cursor.toArray())


def _row_value(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row[name]
    return getattr(row, name)


class DurableObjectSqlStateStore:
    """Persist logical Workgate JSON state in one actor-owned SQLite database.

    The adapter intentionally preserves the existing synchronous StateStore
    contract. Hosted actor requests cannot interleave while one synchronous
    transaction scope is running, so no async KV facade or durable command
    queue is needed.
    """

    def __init__(
        self,
        sql: SqlStorage,
        *,
        logical_root: Path = _DEFAULT_LOGICAL_ROOT,
    ) -> None:
        self._sql = sql
        self._layout = StateLayout(logical_root)
        self._sql.exec(
            """
            CREATE TABLE IF NOT EXISTS workgate_state (
                path TEXT PRIMARY KEY NOT NULL,
                json TEXT NOT NULL
            )
            """
        )

    @property
    def layout(self) -> StateLayout:
        """Return the synthetic path layout used as logical state identities."""
        return self._layout

    def _key(self, path: Path, *, allow_root: bool = False) -> str:
        root = self._layout.root
        candidate = path
        if not candidate.is_absolute() and not candidate.is_relative_to(root):
            candidate = root / candidate
        if root.is_absolute() != candidate.is_absolute():
            raise ValueError(f"state path escapes configured root: {path}")
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"state path escapes configured root: {path}"
            ) from exc
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError(f"invalid state path: {path}")
        if relative == Path("."):
            if allow_root:
                return ""
            raise ValueError("state root is not a JSON value identity")
        key = relative.as_posix()
        return key

    def _all_keys(self) -> tuple[str, ...]:
        cursor = self._sql.exec("SELECT path FROM workgate_state ORDER BY path")
        return tuple(
            str(_row_value(row, "path")) for row in _cursor_rows(cursor)
        )

    def read_json(
        self, path: Path, *, max_bytes: int | None = None
    ) -> Any | None:
        """Read one bounded JSON value by logical state identity."""
        key = self._key(path)
        rows = _cursor_rows(
            self._sql.exec(
                "SELECT json FROM workgate_state WHERE path = ?",
                key,
            )
        )
        if not rows:
            return None
        encoded = str(_row_value(rows[0], "json"))
        size = len(encoded.encode("utf-8"))
        if max_bytes is not None and size > max_bytes:
            raise ValueError(
                f"Refusing to read {size} state bytes; max is {max_bytes}"
            )
        return json.loads(encoded)

    def write_json(self, path: Path, value: Any) -> None:
        """Atomically replace one deterministic JSON value."""
        key = self._key(path)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        self._sql.exec(
            """
            INSERT INTO workgate_state(path, json)
            VALUES (?, ?)
            ON CONFLICT(path) DO UPDATE SET json = excluded.json
            """,
            key,
            encoded,
        )

    def remove(self, path: Path, *, recursive: bool = False) -> None:
        """Remove one logical JSON value or a complete logical subtree."""
        key = self._key(path, allow_root=True)
        prefix = "" if not key else key.rstrip("/") + "/"
        keys = self._all_keys()
        if recursive:
            for candidate in keys:
                if not key or candidate == key or candidate.startswith(prefix):
                    self._sql.exec(
                        "DELETE FROM workgate_state WHERE path = ?",
                        candidate,
                    )
            return
        if (not key and keys) or any(
            candidate.startswith(prefix) for candidate in keys
        ):
            raise OSError(f"state directory is not empty: {path}")
        if key:
            self._sql.exec("DELETE FROM workgate_state WHERE path = ?", key)

    def iter_directories(self, path: Path) -> Iterable[Path]:
        """Return logical child directories represented by stored descendants."""
        base_key = self._key(path, allow_root=True)
        prefix = "" if not base_key else base_key.rstrip("/") + "/"
        children: set[str] = set()
        for candidate in self._all_keys():
            if not candidate.startswith(prefix):
                continue
            remainder = candidate[len(prefix) :]
            if "/" not in remainder:
                continue
            children.add(remainder.split("/", 1)[0])
        return tuple(path / name for name in sorted(children))

    @contextmanager
    def transaction(self, path: Path) -> Generator[None]:
        """Serialize one synchronous actor-local read/modify/write scope."""
        self._key(path, allow_root=True)
        yield
