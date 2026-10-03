"""Control-owned durable semantic task state with Todo compatibility."""

import asyncio
import base64
import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..audit import audit
from ..config.control import ControlConfig
from ..oauth.core.context import current_oauth_claims
from ..persistence import StateStore
from ..protocol.ids import TaskId, new_task_id
from ..schemas.result_models.task import (
    TaskDeleteOutput,
    TaskDocument,
    TaskOutput,
    TaskPlan,
    TaskPlanStep,
    TaskProgress,
)
from ..schemas.result_models.todo import (
    ReadTodosOutput,
    TodoItem,
    WriteTodosOutput,
)
from .state import ControlSessionRecord, ControlState

logger = logging.getLogger(__name__)

_PLAN_STEP_STATUSES = frozenset(
    {"pending", "in_progress", "completed", "skipped", "blocked"}
)
_REPORT_LIST_LIMIT = 50
_TEXT_MAX_BYTES = 20_000
_LABEL_MAX_BYTES = 256
_STEP_ID_MAX_BYTES = 256
_STEP_CONTENT_MAX_BYTES = 16_384
_STEP_LABEL_MAX_BYTES = 64
_TASK_HISTORY_LIMIT_PER_PRINCIPAL = 256
_TASK_TERMINAL_RETENTION_S = 30 * 24 * 60 * 60


class _StoredTask(BaseModel):
    """Private persistence envelope for one semantic task."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    task_id: TaskId
    subject: str = Field(min_length=1, max_length=512)
    document: TaskDocument


class ControlTaskService:
    """Own semantic task lifecycle independently of execution sessions."""

    def __init__(
        self,
        state: ControlState,
        store: StateStore,
        settings: ControlConfig,
    ) -> None:
        self._state = state
        self._store = store
        self._settings = settings

    @staticmethod
    def _subject() -> str:
        claims = current_oauth_claims()
        subject = str(claims.get("sub") or "").strip() if claims else ""
        return subject or "local-user"

    def _path(self, task_id: str):
        return self._store.layout.control_task_path(task_id)

    def _legacy_path(self, session_id: str):
        return self._store.layout.control_task_state_path(session_id)

    @staticmethod
    def _legacy_task_id(session_id: str) -> str:
        digest = hashlib.sha256(session_id.encode("utf-8")).digest()[:16]
        token = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return f"task_{token}"

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
    ) -> list[TaskPlanStep]:
        if len(steps) > self._settings.max_todos:
            raise ValueError(
                f"Refusing to write {len(steps)} plan steps; "
                f"max is {self._settings.max_todos}"
            )
        normalized: list[TaskPlanStep] = []
        seen: set[str] = set()
        allowed_fields = {"id", "content", "status", "priority"}
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
                TaskPlanStep(
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
                )
            )
        return normalized

    def _normalize_legacy_todos(
        self, todos: list[dict[str, Any]]
    ) -> list[TaskPlanStep]:
        """Preserve the established write_todos compatibility semantics."""
        if len(todos) > self._settings.max_todos:
            raise ValueError(
                f"Refusing to write {len(todos)} todos; "
                f"max is {self._settings.max_todos}"
            )
        return [
            TaskPlanStep(
                id=str(item.get("id") or index + 1),
                content=str(item.get("content") or ""),
                status=str(item.get("status") or "pending"),
                priority=str(item.get("priority") or "medium"),
            )
            for index, item in enumerate(todos)
        ]

    @staticmethod
    def _legacy_document(
        value: dict[str, Any],
        *,
        created_at: float,
        label: str | None,
    ) -> TaskDocument:
        updated_at = float(value.get("updated_at") or created_at)
        if "todos" in value and "plan" not in value:
            legacy = ReadTodosOutput.model_validate(value)
            plan = TaskPlan(
                steps=[
                    TaskPlanStep(
                        id=item.id,
                        content=item.content,
                        status=item.status,
                        priority=item.priority,
                    )
                    for item in legacy.todos
                ]
            )
            updated_at = float(legacy.updated_at or created_at)
            objective = None
            status = "active"
            progress = TaskProgress()
        else:
            objective = (
                str(value["objective"])
                if value.get("objective") is not None
                else None
            )
            status = str(value.get("status") or "active")
            progress = TaskProgress.model_validate(value.get("progress") or {})
            plan = TaskPlan.model_validate(value.get("plan") or {})
        return TaskDocument.model_validate(
            {
                "created_at": created_at,
                "updated_at": updated_at,
                "label": label,
                "objective": objective,
                "status": status,
                "progress": progress,
                "plan": plan,
            }
        )

    def _read_stored_unlocked(self, task_id: str) -> _StoredTask:
        value = self._store.read_json(
            self._path(task_id), max_bytes=self._settings.max_todo_bytes
        )
        if value is None:
            raise ValueError(f"unknown task_id {task_id!r}")
        try:
            stored = _StoredTask.model_validate(value)
        except ValidationError as exc:
            raise RuntimeError(
                f"invalid durable task state for {task_id}"
            ) from exc
        if stored.subject != self._subject():
            raise PermissionError("task belongs to a different principal")
        return stored

    def _session_ids(self, task_id: str) -> list[str]:
        records = [
            record
            for record in self._state.snapshot_sessions().values()
            if str(record.task_id or "") == task_id
        ]
        records.sort(
            key=lambda record: (record.created_at, str(record.session_id))
        )
        return [str(record.session_id) for record in records]

    def _task_output(self, stored: _StoredTask) -> TaskOutput:
        return TaskOutput(
            **stored.document.model_dump(mode="python"),
            task_id=str(stored.task_id),
            session_ids=self._session_ids(str(stored.task_id)),
        )

    def _persist_stored(self, path: Any, stored: _StoredTask) -> None:
        data = stored.model_dump(mode="json")
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
                f"Refusing to write {encoded_bytes} task bytes; "
                f"max is {self._settings.max_todo_bytes}"
            )
        self._store.write_json(path, data)

    @staticmethod
    def _require_mutable(document: TaskDocument) -> None:
        if document.status == "cancelled":
            raise ValueError("cancelled task is terminal")
        if document.status == "completed":
            raise ValueError("completed task must be resumed before mutation")

    @staticmethod
    def _require_completable(document: TaskDocument) -> None:
        unfinished = [
            step.id
            for step in document.plan.steps
            if step.status not in {"completed", "skipped"}
        ]
        if unfinished:
            raise ValueError(
                "cannot finish task while unfinished plan steps remain: "
                + ", ".join(unfinished)
            )

    @staticmethod
    def _audit_mutation(
        task_id: str,
        *,
        operation: str,
        changed_fields: list[str],
        session_ids: list[str],
        step_changes: list[dict[str, str]] | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "task": task_id,
            "operation": operation,
            "changed_fields": changed_fields,
        }
        if session_ids:
            fields["session"] = session_ids[0]
            fields["session_ids"] = session_ids
        if step_changes:
            fields["step_changes"] = step_changes
        try:
            audit("task_mutation", **fields)
        except Exception:
            logger.exception("Failed to append task mutation audit event")

    def _mutate_sync(
        self,
        task_id: str,
        operation: str,
        mutate: Callable[
            [TaskDocument], tuple[list[str], list[dict[str, str]]]
        ],
    ) -> TaskOutput:
        path = self._path(task_id)
        with self._store.transaction(path):
            current = self._read_stored_unlocked(task_id)
            document = current.document.model_copy(deep=True)
            changed_fields, step_changes = mutate(document)
            document.updated_at = time.time()
            stored = current.model_copy(update={"document": document})
            self._persist_stored(path, stored)
        session_ids = self._session_ids(task_id)
        self._audit_mutation(
            task_id,
            operation=operation,
            changed_fields=changed_fields,
            session_ids=session_ids,
            step_changes=step_changes,
        )
        return self._task_output(stored)

    async def _mutate(
        self,
        task_id: str,
        operation: str,
        mutate: Callable[
            [TaskDocument], tuple[list[str], list[dict[str, str]]]
        ],
    ) -> TaskOutput:
        return await asyncio.to_thread(
            self._mutate_sync,
            task_id,
            operation,
            mutate,
        )

    def _iter_subject_tasks(self, subject: str) -> list[_StoredTask]:
        root = self._store.layout.control_tasks_dir
        tasks: list[_StoredTask] = []
        for path in self._store.iter_files(root):
            if not path.name.startswith("task_") or path.suffix != ".json":
                continue
            value = self._store.read_json(
                path, max_bytes=self._settings.max_todo_bytes
            )
            if value is None:
                continue
            try:
                stored = _StoredTask.model_validate(value)
            except ValidationError:
                continue
            if stored.subject == subject:
                tasks.append(stored)
        return tasks

    def _has_live_session(self, task_id: str) -> bool:
        return any(
            str(record.task_id or "") == task_id
            and str(record.status) != "ended"
            for record in self._state.snapshot_sessions().values()
        )

    def _remove_retained_terminal(
        self,
        task_id: str,
        *,
        stale_before: float | None = None,
    ) -> bool:
        """Remove one still-terminal task under its own mutation lock."""
        path = self._path(task_id)
        with self._store.transaction(path):
            value = self._store.read_json(
                path, max_bytes=self._settings.max_todo_bytes
            )
            if value is None:
                return True
            try:
                current = _StoredTask.model_validate(value)
            except ValidationError:
                return False
            if current.subject != self._subject():
                return False
            if current.document.status not in {"completed", "cancelled"}:
                return False
            if self._has_live_session(task_id):
                return False
            if (
                stale_before is not None
                and current.document.updated_at >= stale_before
            ):
                return False
            self._state.detach_task(task_id)
            self._store.remove(path)
        return True

    def _prune_for_create(self, subject: str) -> None:
        tasks = self._iter_subject_tasks(subject)
        terminal = sorted(
            (
                task
                for task in tasks
                if task.document.status in {"completed", "cancelled"}
            ),
            key=lambda task: (
                task.document.updated_at,
                task.document.created_at,
                str(task.task_id),
            ),
        )
        remaining = {str(task.task_id) for task in tasks}
        stale_before = time.time() - _TASK_TERMINAL_RETENTION_S
        for task in terminal:
            task_id = str(task.task_id)
            if task.document.updated_at >= stale_before:
                continue
            if self._remove_retained_terminal(
                task_id, stale_before=stale_before
            ):
                remaining.discard(task_id)
        if len(remaining) >= _TASK_HISTORY_LIMIT_PER_PRINCIPAL:
            for task in terminal:
                task_id = str(task.task_id)
                if task_id not in remaining:
                    continue
                if self._remove_retained_terminal(task_id):
                    remaining.discard(task_id)
                if len(remaining) < _TASK_HISTORY_LIMIT_PER_PRINCIPAL:
                    break
        if len(remaining) >= _TASK_HISTORY_LIMIT_PER_PRINCIPAL:
            raise RuntimeError(
                "task history limit reached; finish or cancel existing tasks "
                "before creating another"
            )

    def _create_sync(
        self, *, label: str | None, objective: str | None
    ) -> TaskOutput:
        subject = self._subject()
        registry_path = self._store.layout.control_tasks_dir / ".registry"
        with self._store.transaction(registry_path):
            self._prune_for_create(subject)
            now = time.time()
            task_id = str(new_task_id())
            document = TaskDocument(
                created_at=now,
                updated_at=now,
                label=self._optional_text(
                    label, field="label", max_bytes=_LABEL_MAX_BYTES
                )
                if label is not None
                else None,
                objective=self._optional_text(objective, field="objective")
                if objective is not None
                else None,
            )
            stored = _StoredTask(
                task_id=task_id,
                subject=subject,
                document=document,
            )
            self._persist_stored(self._path(task_id), stored)
        self._audit_mutation(
            task_id,
            operation="create",
            changed_fields=["label", "objective"],
            session_ids=[],
        )
        return self._task_output(stored)

    async def create_task(
        self, *, label: str | None = None, objective: str | None = None
    ) -> TaskOutput:
        return await asyncio.to_thread(
            self._create_sync, label=label, objective=objective
        )

    async def read_task(self, task_id: str) -> TaskOutput:
        return await asyncio.to_thread(
            lambda: self._task_output(self._read_stored_unlocked(task_id))
        )

    def admit_session_attachment(self, record: ControlSessionRecord) -> None:
        task_id = str(record.task_id or "")
        if not task_id:
            raise ValueError("task-linked session admission requires task_id")
        with self._store.transaction(self._path(task_id)):
            task = self._read_stored_unlocked(task_id)
            if task.document.status == "completed":
                raise ValueError(
                    "cannot attach a new execution session to a completed task; "
                    "resume the task first"
                )
            if task.document.status == "cancelled":
                raise ValueError(
                    "cannot attach a new execution session to a cancelled task"
                )
            self._state.put_session(record)

    async def report_progress(
        self,
        task_id: str,
        *,
        label: str | None = None,
        objective: str | None = None,
        summary: str | None = None,
        findings: list[str] | None = None,
        next_action: str | None = None,
        blockers: list[str] | None = None,
    ) -> TaskOutput:
        provided = {
            "label": label is not None,
            "objective": objective is not None,
            "progress.summary": summary is not None,
            "progress.findings": findings is not None,
            "progress.next_action": next_action is not None,
            "progress.blockers": blockers is not None,
        }
        if not any(provided.values()):
            raise ValueError(
                "task report requires at least one field to update"
            )

        def mutate(
            document: TaskDocument,
        ) -> tuple[list[str], list[dict[str, str]]]:
            self._require_mutable(document)
            changed = [name for name, present in provided.items() if present]
            if label is not None:
                document.label = self._optional_text(
                    label, field="label", max_bytes=_LABEL_MAX_BYTES
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
            return changed, []

        return await self._mutate(task_id, "report", mutate)

    async def _set_status(
        self,
        task_id: str,
        *,
        action: Literal["block", "resume", "finish", "cancel"],
    ) -> TaskOutput:
        def mutate(
            document: TaskDocument,
        ) -> tuple[list[str], list[dict[str, str]]]:
            current = document.status
            if action == "block":
                if current != "active":
                    raise ValueError(f"cannot block a {current} task")
                document.status = "blocked"
            elif action == "resume":
                if current not in {"blocked", "completed"}:
                    raise ValueError(f"cannot resume a {current} task")
                document.status = "active"
            elif action == "finish":
                if current not in {"active", "blocked"}:
                    raise ValueError(f"cannot finish a {current} task")
                self._require_completable(document)
                document.status = "completed"
            else:
                if current not in {"active", "blocked"}:
                    raise ValueError(f"cannot cancel a {current} task")
                document.status = "cancelled"
            return ["status"], []

        return await self._mutate(task_id, action, mutate)

    async def block_task(self, task_id: str) -> TaskOutput:
        return await self._set_status(task_id, action="block")

    async def resume_task(self, task_id: str) -> TaskOutput:
        return await self._set_status(task_id, action="resume")

    async def finish_task(self, task_id: str) -> TaskOutput:
        return await self._set_status(task_id, action="finish")

    async def cancel_task(self, task_id: str) -> TaskOutput:
        return await self._set_status(task_id, action="cancel")

    def _delete_sync(self, task_id: str) -> TaskDeleteOutput:
        path = self._path(task_id)
        with self._store.transaction(path):
            current = self._read_stored_unlocked(task_id)
            if current.document.status not in {"completed", "cancelled"}:
                raise ValueError(
                    "only completed or cancelled tasks may be deleted"
                )
            self._state.detach_task(task_id)
            self._store.remove(path)
        try:
            audit("task_deleted", task=task_id)
        except Exception:
            logger.exception("Failed to append task deletion audit event")
        return TaskDeleteOutput(task_id=task_id)

    async def delete_task(self, task_id: str) -> TaskDeleteOutput:
        return await asyncio.to_thread(self._delete_sync, task_id)

    async def update_plan(
        self,
        task_id: str,
        *,
        steps: list[dict[str, Any]],
    ) -> TaskOutput:
        replacement = self._normalize_steps(steps)

        def mutate(
            document: TaskDocument,
        ) -> tuple[list[str], list[dict[str, str]]]:
            self._require_mutable(document)
            document.plan.steps = replacement
            return (
                ["plan.steps"],
                [
                    {"id": step.id, "status": step.status}
                    for step in replacement
                ],
            )

        return await self._mutate(task_id, "plan_replace", mutate)

    @staticmethod
    def _todo_output(
        document: TaskDocument, *, write: bool
    ) -> ReadTodosOutput | WriteTodosOutput:
        model = WriteTodosOutput if write else ReadTodosOutput
        return model(
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

    async def read(self, task_id: str) -> ReadTodosOutput:
        task = await self.read_task(task_id)
        return self._todo_output(task, write=False)  # type: ignore[return-value]

    async def _write_task_output(
        self,
        task_id: str,
        todos: list[dict[str, Any]],
    ) -> TaskOutput:
        normalized = self._normalize_legacy_todos(todos)

        def mutate(
            document: TaskDocument,
        ) -> tuple[list[str], list[dict[str, str]]]:
            self._require_mutable(document)
            document.plan.steps = normalized
            return (
                ["plan.steps"],
                [{"id": step.id, "status": step.status} for step in normalized],
            )

        return await self._mutate(
            task_id,
            "todo_replace",
            mutate,
        )

    async def write(
        self,
        task_id: str,
        todos: list[dict[str, Any]],
    ) -> WriteTodosOutput:
        task = await self._write_task_output(task_id, todos)
        return self._todo_output(task, write=True)  # type: ignore[return-value]

    async def read_with_task(
        self, task_id: str
    ) -> tuple[ReadTodosOutput, TaskOutput]:
        task = await self.read_task(task_id)
        return self._todo_output(task, write=False), task  # type: ignore[return-value]

    async def write_with_task(
        self,
        task_id: str,
        todos: list[dict[str, Any]],
    ) -> tuple[WriteTodosOutput, TaskOutput]:
        task = await self._write_task_output(task_id, todos)
        return self._todo_output(task, write=True), task  # type: ignore[return-value]

    def _migrate_legacy_sync(self) -> int:
        migrated = 0
        for record in sorted(
            self._state.snapshot_sessions().values(),
            key=lambda item: (item.created_at, str(item.session_id)),
        ):
            session_id = str(record.session_id)
            legacy_path = self._legacy_path(session_id)
            task_id = self._legacy_task_id(session_id)
            task_path = self._path(task_id)
            if record.task_id is not None:
                if str(record.task_id) == task_id:
                    with self._store.transaction(task_path):
                        stored_value = self._store.read_json(
                            task_path, max_bytes=self._settings.max_todo_bytes
                        )
                        if stored_value is not None:
                            _StoredTask.model_validate(stored_value)
                            self._store.remove(legacy_path)
                continue
            value = self._store.read_json(
                legacy_path, max_bytes=self._settings.max_todo_bytes
            )
            if value is None:
                continue
            if not isinstance(value, dict):
                raise RuntimeError(
                    f"invalid legacy task state for session {session_id}"
                )
            with self._store.transaction(task_path):
                stored_value = self._store.read_json(
                    task_path, max_bytes=self._settings.max_todo_bytes
                )
                if stored_value is None:
                    document = self._legacy_document(
                        value,
                        created_at=float(record.created_at),
                        label=record.label,
                    )
                    stored = _StoredTask(
                        task_id=task_id,
                        subject="local-user",
                        document=document,
                    )
                    self._persist_stored(task_path, stored)
                else:
                    _StoredTask.model_validate(stored_value)
                self._state.attach_session_task(session_id, task_id)
                self._store.remove(legacy_path)
            try:
                audit(
                    "task_migrated",
                    task=task_id,
                    session=session_id,
                    session_ids=[session_id],
                )
            except Exception:
                logger.exception("Failed to append task migration audit event")
            migrated += 1
        return migrated

    async def migrate_legacy_sessions(self) -> int:
        """Move legacy session-keyed task documents to deterministic task ids."""
        return await asyncio.to_thread(self._migrate_legacy_sync)
