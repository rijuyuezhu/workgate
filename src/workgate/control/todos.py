"""Control-owned revisioned todo state for shared sessions."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from ..config.settings import Settings
from ..persistence import StateStore
from ..schemas.result_models.todo import (
    ReadTodosOutput,
    TodoItem,
    WriteTodosOutput,
)
from .state import ControlState


class TodoConflictError(RuntimeError):
    """Raised when a revision-guarded replacement targets stale todo state."""


class ControlTodoService:
    """Persist logical session todo state on the control plane."""

    def __init__(
        self, state: ControlState, store: StateStore, settings: Settings
    ) -> None:
        self._state = state
        self._store = store
        self._settings = settings

    def _path(self, session_id: str):
        return self._store.layout.control_dir / "todos" / f"{session_id}.json"

    def _require_active_session(self, session_id: str) -> None:
        record = self._state.snapshot_sessions().get(session_id)
        if record is None or record.status != "active":
            raise ValueError(f"unknown or inactive session_id {session_id!r}")

    def _read_sync(self, session_id: str) -> ReadTodosOutput:
        self._require_active_session(session_id)
        value = self._store.read_json(
            self._path(session_id), max_bytes=self._settings.max_todo_bytes
        )
        if value is None:
            return ReadTodosOutput(revision=0, todos=[])
        return ReadTodosOutput.model_validate(value)

    async def read(self, session_id: str) -> ReadTodosOutput:
        return await asyncio.to_thread(self._read_sync, session_id)

    def _normalized_todos(self, todos: list[dict[str, Any]]) -> list[TodoItem]:
        if len(todos) > self._settings.max_todos:
            raise ValueError(
                f"Refusing to write {len(todos)} todos; max is {self._settings.max_todos}"
            )
        return [
            TodoItem(
                id=str(item.get("id") or index + 1),
                content=str(item.get("content") or ""),
                status=str(item.get("status") or "pending"),
                priority=str(item.get("priority") or "medium"),
            )
            for index, item in enumerate(todos)
        ]

    def _write_sync(
        self,
        session_id: str,
        todos: list[dict[str, Any]],
        expected_revision: int | None,
    ) -> WriteTodosOutput:
        self._require_active_session(session_id)
        if expected_revision is not None and (
            isinstance(expected_revision, bool) or expected_revision < 0
        ):
            raise ValueError("expected_revision must be a non-negative integer")
        normalized = self._normalized_todos(todos)
        path = self._path(session_id)
        with self._store.transaction(path):
            self._require_active_session(session_id)
            current_value = self._store.read_json(
                path, max_bytes=self._settings.max_todo_bytes
            )
            current = (
                ReadTodosOutput(revision=0, todos=[])
                if current_value is None
                else ReadTodosOutput.model_validate(current_value)
            )
            if (
                expected_revision is not None
                and expected_revision != current.revision
            ):
                raise TodoConflictError(
                    "Todo list changed from revision "
                    f"{expected_revision} to {current.revision}; reload before saving"
                )
            payload = WriteTodosOutput(
                revision=current.revision + 1,
                updated_at=time.time(),
                todos=normalized,
            )
            data = payload.model_dump(mode="json")
            encoded_bytes = len(
                json.dumps(
                    data,
                    ensure_ascii=False,
                    allow_nan=False,
                    indent=2,
                    sort_keys=True,
                ).encode("utf-8")
            )
            if encoded_bytes > self._settings.max_todo_bytes:
                raise ValueError(
                    f"Refusing to write {encoded_bytes} todo bytes; "
                    f"max is {self._settings.max_todo_bytes}"
                )
            self._store.write_json(path, data)
            return payload

    async def write(
        self,
        session_id: str,
        todos: list[dict[str, Any]],
        expected_revision: int | None = None,
    ) -> WriteTodosOutput:
        return await asyncio.to_thread(
            self._write_sync, session_id, todos, expected_revision
        )
