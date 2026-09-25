"""Control-owned revisioned task state and Todo compatibility for shared sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..audit import audit
from ..config.control import ControlSettingsView
from ..persistence import StateStore
from ..schemas.result_models.task import (
    SessionPlan,
    SessionPlanStep,
    SessionTaskDocument,
    SessionTaskOutput,
)
from ..schemas.result_models.todo import (
    ReadTodosOutput,
    TodoItem,
    WriteTodosOutput,
)
from .state import ControlState

if TYPE_CHECKING:
    from .sessions import ControlSessionCoordinator


logger = logging.getLogger(__name__)

_TASK_STATUSES = frozenset({"active", "blocked", "completed", "cancelled"})
_PLAN_STEP_STATUSES = frozenset(
    {"pending", "in_progress", "completed", "skipped", "blocked"}
)
_REPORT_LIST_LIMIT = 50
_TEXT_MAX_BYTES = 20_000
_STEP_ID_MAX_BYTES = 256
_STEP_CONTENT_MAX_BYTES = 16_384
_STEP_LABEL_MAX_BYTES = 64
_STEP_NOTE_MAX_BYTES = 16_384


class TodoConflictError(RuntimeError):
    """Raised when a revision-guarded task mutation targets stale state."""


class ControlTodoService:
    """Persist one canonical task document and expose legacy Todo projections."""

    def __init__(
        self,
        state: ControlState,
        store: StateStore,
        settings: ControlSettingsView,
        sessions: ControlSessionCoordinator,
    ) -> None:
        self._state = state
        self._store = store
        self._settings = settings
        self._sessions = sessions

    def _path(self, session_id: str):
        # Keep the established location so existing Todo state is upgraded in place.
        return self._store.layout.control_dir / "todos" / f"{session_id}.json"

    def _require_session(self, session_id: str):
        record = self._state.snapshot_sessions().get(session_id)
        if record is None:
            raise ValueError(f"unknown session_id {session_id!r}")
        return record

    def _require_writable_session(self, session_id: str):
        record = self._require_session(session_id)
        if record.status != "active":
            raise ValueError(
                f"session_id {session_id!r} is {record.status}; "
                "durable task state is read-only"
            )
        return record

    @staticmethod
    def _bounded_text(
        value: Any,
        *,
        field: str,
        max_bytes: int,
        allow_empty: bool = True,
    ) -> str:
        normalized = str(value if value is not None else "")
        if not normalized and not allow_empty:
            raise ValueError(f"{field} must not be empty")
        if len(normalized.encode("utf-8")) > max_bytes:
            raise ValueError(f"{field} exceeds {max_bytes} encoded bytes")
        return normalized

    @classmethod
    def _optional_text(
        cls, value: Any, *, field: str, max_bytes: int = _TEXT_MAX_BYTES
    ) -> str | None:
        normalized = cls._bounded_text(
            value, field=field, max_bytes=max_bytes
        ).strip()
        return normalized or None

    @classmethod
    def _report_list(cls, value: list[str], *, field: str) -> list[str]:
        if len(value) > _REPORT_LIST_LIMIT:
            raise ValueError(
                f"{field} may contain at most {_REPORT_LIST_LIMIT} items"
            )
        normalized: list[str] = []
        for index, item in enumerate(value):
            text = cls._optional_text(
                item, field=f"{field}[{index}]", max_bytes=_TEXT_MAX_BYTES
            )
            if text is not None:
                normalized.append(text)
        return normalized

    def _normalize_steps(
        self, steps: list[dict[str, Any]]
    ) -> list[SessionPlanStep]:
        if len(steps) > self._settings.max_todos:
            raise ValueError(
                f"Refusing to write {len(steps)} plan steps; "
                f"max is {self._settings.max_todos}"
            )
        normalized: list[SessionPlanStep] = []
        seen: set[str] = set()
        allowed_fields = {"id", "content", "status", "priority", "note"}
        for index, item in enumerate(steps):
            if not isinstance(item, dict):
                raise ValueError(f"steps[{index}] must be a JSON object")
            unknown_fields = sorted(set(item) - allowed_fields)
            if unknown_fields:
                raise ValueError(
                    f"steps[{index}] contains unsupported fields: "
                    + ", ".join(unknown_fields)
                )
            raw_id = item.get("id")
            if not isinstance(raw_id, str) or not raw_id:
                raise ValueError(
                    f"steps[{index}].id is required and must be a string"
                )
            identifier = self._bounded_text(
                raw_id,
                field=f"steps[{index}].id",
                max_bytes=_STEP_ID_MAX_BYTES,
                allow_empty=False,
            ).strip()
            if not identifier:
                raise ValueError(f"steps[{index}].id must not be blank")
            if identifier in seen:
                raise ValueError(f"duplicate plan step id: {identifier}")
            seen.add(identifier)
            status = (
                self._bounded_text(
                    item.get("status") or "pending",
                    field=f"steps[{index}].status",
                    max_bytes=_STEP_LABEL_MAX_BYTES,
                    allow_empty=False,
                )
                .strip()
                .lower()
            )
            if status not in _PLAN_STEP_STATUSES:
                allowed = ", ".join(sorted(_PLAN_STEP_STATUSES))
                raise ValueError(
                    f"unsupported plan step status {status!r}; "
                    f"expected one of: {allowed}"
                )
            normalized.append(
                SessionPlanStep(
                    id=identifier,
                    content=self._bounded_text(
                        item.get("content") or "",
                        field=f"steps[{index}].content",
                        max_bytes=_STEP_CONTENT_MAX_BYTES,
                    ),
                    status=status,
                    priority=self._bounded_text(
                        item.get("priority") or "medium",
                        field=f"steps[{index}].priority",
                        max_bytes=_STEP_LABEL_MAX_BYTES,
                        allow_empty=False,
                    ),
                    note=self._optional_text(
                        item.get("note"),
                        field=f"steps[{index}].note",
                        max_bytes=_STEP_NOTE_MAX_BYTES,
                    ),
                )
            )
        return normalized

    def _normalize_legacy_todos(
        self, todos: list[dict[str, Any]]
    ) -> list[SessionPlanStep]:
        """Preserve the pre-task-state write_todos input semantics."""
        if len(todos) > self._settings.max_todos:
            raise ValueError(
                f"Refusing to write {len(todos)} todos; "
                f"max is {self._settings.max_todos}"
            )
        return [
            SessionPlanStep(
                id=str(item.get("id") or index + 1),
                content=str(item.get("content") or ""),
                status=str(item.get("status") or "pending"),
                priority=str(item.get("priority") or "medium"),
            )
            for index, item in enumerate(todos)
        ]

    @staticmethod
    def _legacy_document(value: dict[str, Any]) -> SessionTaskDocument:
        legacy = ReadTodosOutput.model_validate(value)
        return SessionTaskDocument(
            revision=legacy.revision,
            updated_at=legacy.updated_at,
            plan=SessionPlan(
                steps=[
                    SessionPlanStep(
                        id=item.id,
                        content=item.content,
                        status=item.status,
                        priority=item.priority,
                    )
                    for item in legacy.todos
                ]
            ),
        )

    def _document_from_value(self, value: Any | None) -> SessionTaskDocument:
        if value is None:
            return SessionTaskDocument()
        if isinstance(value, dict) and "todos" in value and "plan" not in value:
            return self._legacy_document(value)
        return SessionTaskDocument.model_validate(value)

    def _read_document_unlocked(self, session_id: str) -> SessionTaskDocument:
        value = self._store.read_json(
            self._path(session_id), max_bytes=self._settings.max_todo_bytes
        )
        return self._document_from_value(value)

    @staticmethod
    def _todo_output(
        document: SessionTaskDocument, *, write: bool
    ) -> ReadTodosOutput | WriteTodosOutput:
        model = WriteTodosOutput if write else ReadTodosOutput
        return model(
            revision=document.revision,
            updated_at=document.updated_at,
            todos=[
                TodoItem(
                    id=step.id,
                    content=step.content,
                    status=step.status,
                    priority=step.priority,
                )
                for step in document.plan.steps
            ],
        )

    @staticmethod
    def _task_output(
        record: Any, document: SessionTaskDocument
    ) -> SessionTaskOutput:
        return SessionTaskOutput(
            **document.model_dump(mode="python"),
            session_id=str(record.session_id),
            label=record.label,
            execution_status=str(record.status),
        )

    def _read_with_task_sync(
        self, session_id: str
    ) -> tuple[ReadTodosOutput, SessionTaskOutput]:
        record = self._require_session(session_id)
        document = self._read_document_unlocked(session_id)
        return (
            self._todo_output(document, write=False),  # type: ignore[return-value]
            self._task_output(record, document),
        )

    def _read_sync(self, session_id: str) -> ReadTodosOutput:
        todos, _task = self._read_with_task_sync(session_id)
        return todos

    async def read(self, session_id: str) -> ReadTodosOutput:
        """Read the Todo compatibility projection, including for ended sessions."""
        return await asyncio.to_thread(self._read_sync, session_id)

    def _read_task_sync(self, session_id: str) -> SessionTaskOutput:
        _todos, task = self._read_with_task_sync(session_id)
        return task

    async def read_task(self, session_id: str) -> SessionTaskOutput:
        """Read canonical task state without requiring executor availability."""
        return await asyncio.to_thread(self._read_task_sync, session_id)

    async def read_with_task(
        self, session_id: str
    ) -> tuple[ReadTodosOutput, SessionTaskOutput]:
        """Read Todo and task projections from one canonical document snapshot."""
        return await asyncio.to_thread(self._read_with_task_sync, session_id)

    @staticmethod
    def _require_expected_revision(
        current: SessionTaskDocument, expected_revision: int | None
    ) -> None:
        if expected_revision is None:
            return
        if isinstance(expected_revision, bool) or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if expected_revision != current.revision:
            raise TodoConflictError(
                "Session task changed from revision "
                f"{expected_revision} to {current.revision}; reload before saving"
            )

    @staticmethod
    def _require_task_mutable(
        document: SessionTaskDocument, *, resuming: bool = False
    ) -> None:
        if document.status == "cancelled":
            raise ValueError("cancelled session task is terminal")
        if document.status == "completed" and not resuming:
            raise ValueError(
                "completed session task must be explicitly resumed with "
                "task_status='active' before further updates"
            )

    @staticmethod
    def _require_completable(document: SessionTaskDocument) -> None:
        unfinished = [
            step.id
            for step in document.plan.steps
            if step.status not in {"completed", "skipped"}
        ]
        if unfinished:
            raise ValueError(
                "cannot complete session task while unfinished plan steps remain: "
                + ", ".join(unfinished)
            )

    def _persist_document(
        self, path: Any, document: SessionTaskDocument
    ) -> None:
        data = document.model_dump(mode="json")
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
                f"Refusing to write {encoded_bytes} session-task bytes; "
                f"max is {self._settings.max_todo_bytes}"
            )
        self._store.write_json(path, data)

    @staticmethod
    def _audit_mutation(
        session_id: str,
        *,
        operation: str,
        revision: int,
        changed_fields: list[str],
        step_changes: list[dict[str, str]] | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "session": session_id,
            "operation": operation,
            "revision": revision,
            "changed_fields": changed_fields,
        }
        if step_changes:
            fields["step_changes"] = step_changes
        try:
            audit("session_task_mutation", **fields)
        except Exception:
            # The canonical write is already committed; never turn a successful
            # revision into an ambiguous client-visible failure because a
            # secondary audit append failed.
            logger.exception(
                "Failed to append session task mutation audit event"
            )

    def _mutate_sync(
        self,
        session_id: str,
        expected_revision: int | None,
        operation: str,
        mutate: Callable[
            [SessionTaskDocument], tuple[list[str], list[dict[str, str]]]
        ],
    ) -> SessionTaskOutput:
        record = self._require_writable_session(session_id)
        path = self._path(session_id)
        with self._store.transaction(path):
            record = self._require_writable_session(session_id)
            current = self._read_document_unlocked(session_id)
            self._require_expected_revision(current, expected_revision)
            document = current.model_copy(deep=True)
            changed_fields, step_changes = mutate(document)
            document.revision = current.revision + 1
            document.updated_at = time.time()
            self._persist_document(path, document)
        self._audit_mutation(
            session_id,
            operation=operation,
            revision=document.revision,
            changed_fields=changed_fields,
            step_changes=step_changes,
        )
        return self._task_output(record, document)

    async def _mutate(
        self,
        session_id: str,
        expected_revision: int | None,
        operation: str,
        mutate: Callable[
            [SessionTaskDocument], tuple[list[str], list[dict[str, str]]]
        ],
    ) -> SessionTaskOutput:
        # Hold the execution-session lifecycle lock without requiring the executor
        # to be online. session_end therefore cannot race a task write.
        async with self._sessions.session_admission(
            (session_id,), require_available=()
        ):
            return await asyncio.to_thread(
                self._mutate_sync,
                session_id,
                expected_revision,
                operation,
                mutate,
            )

    async def _write_task_output(
        self,
        session_id: str,
        todos: list[dict[str, Any]],
        expected_revision: int | None,
    ) -> SessionTaskOutput:
        normalized = self._normalize_legacy_todos(todos)

        def mutate(
            document: SessionTaskDocument,
        ) -> tuple[list[str], list[dict[str, str]]]:
            self._require_task_mutable(document)
            existing_notes = {
                step.id: step.note for step in document.plan.steps
            }
            replacement_steps = [
                step.model_copy(update={"note": existing_notes.get(step.id)})
                for step in normalized
            ]
            document.plan.steps = replacement_steps
            return (
                ["plan.steps"],
                [
                    {"id": step.id, "status": step.status}
                    for step in replacement_steps
                ],
            )

        return await self._mutate(
            session_id, expected_revision, "todo_replace", mutate
        )

    @staticmethod
    def _todo_write_output(output: SessionTaskOutput) -> WriteTodosOutput:
        document = SessionTaskDocument.model_validate(
            output.model_dump(
                exclude={"session_id", "label", "execution_status"}
            )
        )
        return ControlTodoService._todo_output(  # type: ignore[return-value]
            document, write=True
        )

    async def write(
        self,
        session_id: str,
        todos: list[dict[str, Any]],
        expected_revision: int | None = None,
    ) -> WriteTodosOutput:
        """Replace plan steps through the legacy Todo compatibility surface."""
        output = await self._write_task_output(
            session_id, todos, expected_revision
        )
        return self._todo_write_output(output)

    async def write_with_task(
        self,
        session_id: str,
        todos: list[dict[str, Any]],
        expected_revision: int | None = None,
    ) -> tuple[WriteTodosOutput, SessionTaskOutput]:
        """Replace Todos and return both projections from the committed revision."""
        task = await self._write_task_output(
            session_id, todos, expected_revision
        )
        return self._todo_write_output(task), task

    async def report_progress(
        self,
        session_id: str,
        *,
        expected_revision: int,
        objective: str | None = None,
        summary: str | None = None,
        findings: list[str] | None = None,
        next_action: str | None = None,
        blockers: list[str] | None = None,
        task_status: str | None = None,
    ) -> SessionTaskOutput:
        provided = {
            "objective": objective is not None,
            "progress.summary": summary is not None,
            "progress.findings": findings is not None,
            "progress.next_action": next_action is not None,
            "progress.blockers": blockers is not None,
            "status": task_status is not None,
        }
        if not any(provided.values()):
            raise ValueError(
                "report_session_progress requires at least one field to update"
            )
        normalized_status = None
        if task_status is not None:
            normalized_status = (
                self._bounded_text(
                    task_status,
                    field="task_status",
                    max_bytes=_STEP_LABEL_MAX_BYTES,
                    allow_empty=False,
                )
                .strip()
                .lower()
            )
            if normalized_status not in _TASK_STATUSES:
                allowed = ", ".join(sorted(_TASK_STATUSES))
                raise ValueError(
                    f"unsupported task_status {normalized_status!r}; "
                    f"expected one of: {allowed}"
                )

        def mutate(
            document: SessionTaskDocument,
        ) -> tuple[list[str], list[dict[str, str]]]:
            resuming = (
                document.status == "completed" and normalized_status == "active"
            )
            self._require_task_mutable(document, resuming=resuming)
            changed = [name for name, present in provided.items() if present]
            progress_changed = any(
                name.startswith("progress.") and present
                for name, present in provided.items()
            )
            if objective is not None:
                document.objective = self._optional_text(
                    objective, field="objective"
                )
            if summary is not None:
                document.progress.summary = self._optional_text(
                    summary, field="summary"
                )
            if findings is not None:
                document.progress.findings = self._report_list(
                    findings, field="findings"
                )
            if next_action is not None:
                document.progress.next_action = self._optional_text(
                    next_action, field="next_action"
                )
            if blockers is not None:
                document.progress.blockers = self._report_list(
                    blockers, field="blockers"
                )
            if progress_changed:
                document.progress.updated_at = time.time()
            if normalized_status is not None:
                if normalized_status == "completed":
                    self._require_completable(document)
                document.status = normalized_status  # type: ignore[assignment]
            return changed, []

        return await self._mutate(
            session_id, expected_revision, "progress_report", mutate
        )

    async def update_plan(
        self,
        session_id: str,
        *,
        expected_revision: int,
        steps: list[dict[str, Any]] | None = None,
        step_id: str | None = None,
        status: str | None = None,
        content: str | None = None,
        priority: str | None = None,
        note: str | None = None,
    ) -> SessionTaskOutput:
        replacing = steps is not None
        patching = step_id is not None or any(
            value is not None for value in (status, content, priority, note)
        )
        if replacing and patching:
            raise ValueError(
                "pass either steps for complete replacement or step_id fields, not both"
            )
        if not replacing and not patching:
            raise ValueError(
                "update_session_plan requires steps or step_id plus fields"
            )
        replacement = self._normalize_steps(steps or []) if replacing else None
        normalized_step_id = (
            None
            if step_id is None
            else self._bounded_text(
                step_id,
                field="step_id",
                max_bytes=_STEP_ID_MAX_BYTES,
                allow_empty=False,
            ).strip()
        )
        if step_id is not None and not normalized_step_id:
            raise ValueError("step_id must not be blank")
        if not replacing and normalized_step_id is None:
            raise ValueError("step_id is required for an in-place plan update")
        if not replacing and all(
            value is None for value in (status, content, priority, note)
        ):
            raise ValueError(
                "step_id update requires status, content, priority, or note"
            )
        normalized_status = None
        if status is not None:
            normalized_status = (
                self._bounded_text(
                    status,
                    field="status",
                    max_bytes=_STEP_LABEL_MAX_BYTES,
                    allow_empty=False,
                )
                .strip()
                .lower()
            )
            if normalized_status not in _PLAN_STEP_STATUSES:
                allowed = ", ".join(sorted(_PLAN_STEP_STATUSES))
                raise ValueError(
                    f"unsupported plan step status {normalized_status!r}; "
                    f"expected one of: {allowed}"
                )

        def mutate(
            document: SessionTaskDocument,
        ) -> tuple[list[str], list[dict[str, str]]]:
            self._require_task_mutable(document)
            if replacement is not None:
                document.plan.steps = replacement
                return (
                    ["plan.steps"],
                    [
                        {"id": step.id, "status": step.status}
                        for step in replacement
                    ],
                )
            assert normalized_step_id is not None
            matches = [
                item
                for item in document.plan.steps
                if item.id == normalized_step_id
            ]
            if not matches:
                raise ValueError(f"unknown plan step id: {normalized_step_id}")
            if len(matches) > 1:
                raise ValueError(
                    f"ambiguous plan step id from legacy Todo state: "
                    f"{normalized_step_id}; replace the plan with unique stable ids first"
                )
            target = matches[0]
            changed: list[str] = []
            if normalized_status is not None:
                target.status = normalized_status
                changed.append("status")
            if content is not None:
                target.content = self._bounded_text(
                    content,
                    field="content",
                    max_bytes=_STEP_CONTENT_MAX_BYTES,
                )
                changed.append("content")
            if priority is not None:
                target.priority = self._bounded_text(
                    priority,
                    field="priority",
                    max_bytes=_STEP_LABEL_MAX_BYTES,
                    allow_empty=False,
                )
                changed.append("priority")
            if note is not None:
                target.note = self._optional_text(
                    note, field="note", max_bytes=_STEP_NOTE_MAX_BYTES
                )
                changed.append("note")
            return (
                [
                    f"plan.steps[{normalized_step_id}].{field}"
                    for field in changed
                ],
                [{"id": target.id, "status": target.status}],
            )

        return await self._mutate(
            session_id, expected_revision, "plan_update", mutate
        )
