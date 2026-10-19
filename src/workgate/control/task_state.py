"""Durable control-owned task state with Todo compatibility."""

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..audit import audit
from ..config.control import ControlConfig
from ..config.role_config import use_role_config
from ..errors import PublicToolValueError
from ..oauth.core.context import current_oauth_claims
from ..persistence import StateStore, use_state_store
from ..protocol.ids import TaskId, new_task_id
from ..schemas.result_models.task import (
    TaskDeleteOutput,
    TaskDocument,
    TaskOutput,
    TaskPlanStep,
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
_CONTINUATION_IDLE_S = 15 * 60
_CONTINUATION_MAX_ATTEMPTS = 10
_CONTINUATION_PENDING_TTL_S = 5 * 60
_CONTINUATION_FAILURE_BACKOFF_S = 5 * 60
_CONTINUATION_STORAGE_OVERHEAD_BYTES = 4096


class _TaskContinuation(BaseModel):
    """Private durable automatic-continuation state for one semantic task."""

    model_config = ConfigDict(extra="forbid")

    last_agent_activity: float | None = None
    attempt_count: int = Field(default=0, ge=0)
    pending_claim_id: str | None = None
    pending_since: float | None = None
    reserved: bool = False
    retry_after: float | None = None


class _StoredTask(BaseModel):
    """Private persistence envelope for one task."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    task_id: TaskId
    subject: str = Field(min_length=1, max_length=512)
    document: TaskDocument
    session_count: int | None = Field(default=None, ge=0)
    continuation: _TaskContinuation = Field(default_factory=_TaskContinuation)


class ControlTaskService:
    """Own task state independently of execution sessions."""

    def __init__(
        self,
        state: ControlState,
        store: StateStore,
        settings: ControlConfig,
        *,
        jobs_may_be_active: Callable[[tuple[str, ...]], bool] | None = None,
    ) -> None:
        self._jobs_may_be_active = jobs_may_be_active
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

    @property
    def _stored_task_max_bytes(self) -> int:
        return (
            self._settings.max_todo_bytes + _CONTINUATION_STORAGE_OVERHEAD_BYTES
        )

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

    def _normalize_todos(self, todos: list[Any]) -> list[TaskPlanStep]:
        """Normalize Todo input through the canonical plan validator."""
        steps: list[dict[str, Any]] = []
        for index, item in enumerate(todos):
            if not isinstance(item, dict):
                raise ValueError(f"todos[{index}] must be a JSON object")
            step = dict(item)
            step["id"] = str(item.get("id") or index + 1)
            steps.append(step)
        return self._normalize_steps(steps)

    def _read_stored_unlocked(self, task_id: str) -> _StoredTask:
        value = self._store.read_json(
            self._path(task_id), max_bytes=self._stored_task_max_bytes
        )
        if value is None:
            raise PublicToolValueError(f"unknown task_id {task_id!r}")
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
        continuation = data.pop("continuation")
        session_count = data.pop("session_count")
        user_bytes = len(
            json.dumps(
                data,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
        )
        if user_bytes > self._settings.max_todo_bytes:
            raise ValueError(
                f"Refusing to write {user_bytes} task bytes; "
                f"max is {self._settings.max_todo_bytes}"
            )
        if stored.continuation != _TaskContinuation():
            data["continuation"] = continuation
        data["session_count"] = session_count
        total_bytes = len(
            json.dumps(
                data,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
        )
        if (
            total_bytes
            > self._settings.max_todo_bytes
            + _CONTINUATION_STORAGE_OVERHEAD_BYTES
        ):
            raise ValueError(
                "Refusing oversized private task continuation state"
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
        details: dict[str, Any] | None = None,
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
        if details:
            fields.update(details)
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
            continuation = current.continuation.model_copy(deep=True)
            if (
                continuation.pending_claim_id is not None
                and not continuation.reserved
            ):
                self._clear_continuation_claim(continuation)
            stored = current.model_copy(
                update={
                    "document": document,
                    "continuation": continuation,
                }
            )
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

    @staticmethod
    def _continuation_unfinished(document: TaskDocument) -> bool:
        return bool(document.plan.steps) and any(
            step.status not in {"completed", "skipped"}
            for step in document.plan.steps
        )

    @staticmethod
    def _clear_continuation_claim(
        continuation: _TaskContinuation,
    ) -> None:
        continuation.pending_claim_id = None
        continuation.pending_since = None
        continuation.reserved = False

    @classmethod
    def _continuation_last_activity(cls, stored: _StoredTask) -> float:
        observed = stored.continuation.last_agent_activity
        return max(
            float(stored.document.updated_at),
            float(observed) if observed is not None else 0.0,
        )

    @classmethod
    def _continuation_state(
        cls,
        stored: _StoredTask,
        *,
        now: float,
    ) -> dict[str, Any]:
        continuation = stored.continuation
        last_activity = cls._continuation_last_activity(stored)
        due_at = last_activity + _CONTINUATION_IDLE_S
        if continuation.retry_after is not None:
            due_at = max(due_at, continuation.retry_after)
        exhausted = continuation.attempt_count >= _CONTINUATION_MAX_ATTEMPTS
        unfinished = cls._continuation_unfinished(stored.document)
        eligible = (
            stored.document.status == "active"
            and unfinished
            and not exhausted
            and continuation.pending_claim_id is None
            and now >= due_at
        )
        return {
            "eligible": eligible,
            "pending": continuation.pending_claim_id is not None,
            "pending_expires_at": (
                continuation.pending_since + _CONTINUATION_PENDING_TTL_S
                if continuation.pending_since is not None
                else None
            ),
            "attempt_count": continuation.attempt_count,
            "max_attempts": _CONTINUATION_MAX_ATTEMPTS,
            "due_at": due_at,
            "exhausted": exhausted,
        }

    def _expire_continuation_claim(
        self,
        stored: _StoredTask,
        *,
        now: float,
    ) -> bool:
        continuation = stored.continuation
        if (
            continuation.pending_claim_id is None
            or continuation.pending_since is None
            or now - continuation.pending_since < _CONTINUATION_PENDING_TTL_S
        ):
            return False
        self._clear_continuation_claim(continuation)
        return True

    def _observe_agent_activity_sync(
        self,
        task_ids: tuple[str, ...],
        observed_at: float,
    ) -> None:
        for task_id in dict.fromkeys(task_ids):
            path = self._path(task_id)
            with self._store.transaction(path):
                stored = self._read_stored_unlocked(task_id)
                continuation = stored.continuation.model_copy(deep=True)
                previous = continuation.last_agent_activity
                if previous is not None and observed_at <= previous:
                    continue
                continuation.last_agent_activity = observed_at
                if (
                    continuation.pending_claim_id is not None
                    and not continuation.reserved
                ):
                    self._clear_continuation_claim(continuation)
                updated = stored.model_copy(
                    update={"continuation": continuation}
                )
                self._persist_stored(path, updated)

    async def observe_agent_activity(
        self,
        task_ids: tuple[str, ...],
        *,
        observed_at: float | None = None,
    ) -> None:
        """Persist task-scoped agent activity without changing semantic task state."""
        if not task_ids:
            return
        timestamp = time.time() if observed_at is None else float(observed_at)
        await asyncio.to_thread(
            self._observe_agent_activity_sync,
            task_ids,
            timestamp,
        )

    def _continuation_status_sync(self, task_id: str) -> dict[str, Any]:
        stored = self._read_stored_unlocked(task_id)
        return self._continuation_state(stored, now=time.time())

    async def continuation_status(self, task_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._continuation_status_sync, task_id)

    def _claim_continuation_sync(
        self,
        task_id: str,
    ) -> dict[str, Any]:
        path = self._path(task_id)
        with self._store.transaction(path):
            stored = self._read_stored_unlocked(task_id)
            now = time.time()
            expired = self._expire_continuation_claim(stored, now=now)
            continuation = stored.continuation
            if continuation.pending_claim_id is not None:
                return {
                    "claimed": False,
                    "claim_id": None,
                    "continuation": self._continuation_state(stored, now=now),
                    "task": self._task_output(stored),
                }
            state = self._continuation_state(stored, now=now)
            if not state["eligible"]:
                if expired:
                    self._persist_stored(path, stored)
                return {
                    "claimed": False,
                    "claim_id": None,
                    "continuation": state,
                    "task": self._task_output(stored),
                }
            claim_id = f"c_{secrets.token_hex(8)}"
            continuation.pending_claim_id = claim_id
            continuation.pending_since = now
            continuation.reserved = False
            self._persist_stored(path, stored)
            return {
                "claimed": True,
                "claim_id": claim_id,
                "continuation": self._continuation_state(stored, now=now),
                "task": self._task_output(stored),
            }

    async def claim_continuation(
        self,
        task_id: str,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._claim_continuation_sync,
            task_id,
        )

    def _validate_continuation_sync(
        self,
        task_id: str,
        claim_id: str,
    ) -> dict[str, Any]:
        requested = str(claim_id or "").strip()
        if not requested:
            raise ValueError("claim_id is required")
        path = self._path(task_id)
        with self._store.transaction(path):
            stored = self._read_stored_unlocked(task_id)
            now = time.time()
            continuation = stored.continuation
            changed = self._expire_continuation_claim(stored, now=now)
            valid = bool(
                continuation.pending_claim_id == requested
                and stored.document.status == "active"
                and self._continuation_unfinished(stored.document)
                and (
                    continuation.reserved
                    or continuation.attempt_count < _CONTINUATION_MAX_ATTEMPTS
                )
                and now
                >= self._continuation_last_activity(stored)
                + _CONTINUATION_IDLE_S
            )
            if valid and not continuation.reserved:
                continuation.attempt_count += 1
                continuation.reserved = True
                changed = True
            elif not valid and continuation.pending_claim_id == requested:
                self._clear_continuation_claim(continuation)
                changed = True
            if changed:
                self._persist_stored(path, stored)
            return {
                "valid": valid,
                "claim_id": requested if valid else None,
                "continuation": self._continuation_state(stored, now=now),
                "task": self._task_output(stored),
            }

    async def validate_continuation(
        self,
        task_id: str,
        *,
        claim_id: str,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._validate_continuation_sync,
            task_id,
            claim_id,
        )

    def _report_continuation_sync(
        self,
        task_id: str,
        claim_id: str,
        *,
        accepted: bool,
    ) -> dict[str, Any]:
        requested = str(claim_id or "").strip()
        if not requested:
            raise ValueError("claim_id is required")
        path = self._path(task_id)
        with self._store.transaction(path):
            stored = self._read_stored_unlocked(task_id)
            now = time.time()
            continuation = stored.continuation
            if continuation.pending_claim_id != requested:
                return {
                    "reported": False,
                    "accepted": None,
                    "continuation": self._continuation_state(stored, now=now),
                    "task": self._task_output(stored),
                }
            if not continuation.reserved:
                raise ValueError(
                    "continuation was not reserved for host dispatch"
                )
            if accepted:
                continuation.last_agent_activity = max(
                    now,
                    continuation.last_agent_activity or 0.0,
                )
                continuation.retry_after = None
            else:
                continuation.retry_after = now + _CONTINUATION_FAILURE_BACKOFF_S
            self._clear_continuation_claim(continuation)
            self._persist_stored(path, stored)
            return {
                "reported": True,
                "accepted": bool(accepted),
                "continuation": self._continuation_state(stored, now=now),
                "task": self._task_output(stored),
            }

    async def report_continuation(
        self,
        task_id: str,
        *,
        claim_id: str,
        accepted: bool,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._report_continuation_sync,
            task_id,
            claim_id,
            accepted=accepted,
        )

    def _iter_subject_tasks(self, subject: str) -> list[_StoredTask]:
        root = self._store.layout.control_tasks_dir
        tasks: list[_StoredTask] = []
        for path in self._store.iter_files(root):
            if not path.name.startswith("task_") or path.suffix != ".json":
                continue
            value = self._store.read_json(
                path, max_bytes=self._stored_task_max_bytes
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

    def _sweep_idle_sync(self, now: float) -> int:
        """Block only provably idle Tasks, rechecking under their task locks."""
        idle_s = self._settings.task_idle_block_after_s
        if idle_s == 0:
            return 0
        blocked = 0
        root = self._store.layout.control_tasks_dir
        for path in self._store.iter_files(root):
            if not path.name.startswith("task_") or path.suffix != ".json":
                continue
            with self._store.transaction(path):
                raw = self._store.read_json(
                    path, max_bytes=self._stored_task_max_bytes
                )
                if raw is None:
                    continue
                try:
                    current = _StoredTask.model_validate(raw)
                except ValidationError:
                    continue
                if path != self._path(str(current.task_id)):
                    continue
                document = current.document
                if document.status != "active":
                    continue
                if current.continuation.pending_claim_id is not None:
                    continue
                sessions = tuple(
                    record
                    for record in self._state.snapshot_sessions().values()
                    if str(record.task_id or "") == str(current.task_id)
                )
                # Keep the full attachment count: pruning just one of several
                # Sessions must not make incomplete history look authoritative.
                if current.session_count != len(sessions):
                    continue
                if any(
                    record.status != "ended"
                    or not record.executor_absence_confirmed
                    for record in sessions
                ):
                    continue
                session_ids = tuple(
                    str(record.session_id) for record in sessions
                )
                # Without an authoritative job-store check, only sessionless
                # Tasks are provably safe to transition.
                if session_ids and (
                    self._jobs_may_be_active is None
                    or self._jobs_may_be_active(session_ids)
                ):
                    continue
                last_activity = max(
                    [self._continuation_last_activity(current)]
                    + [record.updated_at for record in sessions]
                )
                if last_activity + idle_s > now:
                    continue
                reason = "Automatically blocked after confirmed inactivity."
                updated_document = document.model_copy(deep=True)
                updated_document.status = "blocked"
                updated_document.updated_at = now
                if (
                    len(updated_document.progress.blockers)
                    >= _REPORT_LIST_LIMIT
                ):
                    # Never discard user blockers or hide the transition reason.
                    continue
                updated_document.progress.blockers.append(reason)
                continuation = current.continuation.model_copy(deep=True)
                self._clear_continuation_claim(continuation)
                self._persist_stored(
                    path,
                    current.model_copy(
                        update={
                            "document": updated_document,
                            "continuation": continuation,
                        }
                    ),
                )
            with use_role_config(self._settings), use_state_store(self._store):
                self._audit_mutation(
                    str(current.task_id),
                    operation="idle_block",
                    changed_fields=["status", "progress.blockers"],
                    session_ids=list(session_ids),
                    details={
                        "reason": reason,
                        "last_activity_at": last_activity,
                        "idle_threshold_s": idle_s,
                    },
                )
            blocked += 1
        return blocked

    async def sweep_idle_tasks(self, *, now: float | None = None) -> int:
        """Apply one bounded observation-based policy pass; no timer state."""
        if self._settings.task_idle_block_after_s == 0:
            return 0
        return await asyncio.to_thread(
            self._sweep_idle_sync, time.time() if now is None else now
        )

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
        """Prune a terminal task if it is still eligible."""
        path = self._path(task_id)
        with self._store.transaction(path):
            value = self._store.read_json(
                path, max_bytes=self._stored_task_max_bytes
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
                session_count=0,
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

    def _list_tasks_sync(self) -> list[TaskOutput]:
        tasks = self._iter_subject_tasks(self._subject())
        tasks.sort(
            key=lambda stored: (
                stored.document.updated_at,
                stored.document.created_at,
                str(stored.task_id),
            ),
            reverse=True,
        )
        return [self._task_output(stored) for stored in tasks]

    def _list_task_summaries_sync(self) -> list[dict[str, Any]]:
        """List only metadata for UI paging; do not expand plans or sessions."""
        return [
            {
                "task_id": str(stored.task_id),
                "label": stored.document.label,
                "objective": stored.document.objective,
                "status": stored.document.status,
                "created_at": stored.document.created_at,
                "updated_at": stored.document.updated_at,
            }
            for stored in self._iter_subject_tasks(self._subject())
        ]

    async def list_task_summaries(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_task_summaries_sync)

    def create_for_session(
        self, record: ControlSessionRecord
    ) -> ControlSessionRecord:
        """Create the semantic Task and publish its Session before Executor admission."""
        # Session labels permit 256 characters; Task labels permit 256 UTF-8
        # bytes. Do not reject an otherwise valid Session because of this.
        label = record.label
        if label is not None and len(label.encode("utf-8")) > _LABEL_MAX_BYTES:
            label = None
        task = self._create_sync(label=label, objective=None)
        attached = record.model_copy(
            update={"task_id": task.task_id, "task_created": True}
        )
        try:
            self.admit_session_attachment(attached)
        except BaseException:
            self.discard_implicit_task(task.task_id)
            raise
        return attached

    def discard_failed_attachment(self, record: ControlSessionRecord) -> None:
        """Undo only an unconfirmed create, preserving incomplete history safely."""
        task_id = str(record.task_id or "")
        path = self._path(task_id)
        with self._store.transaction(path):
            retained = self._state.snapshot_sessions().get(
                str(record.session_id)
            )
            if retained is None or retained.status != "creating":
                return
            if retained.task_id is None:
                # Task was deleted while creation was in flight. The detached
                # Session still needs clearing, but its Task no longer exists.
                self._state.remove_session(str(record.session_id))
                return
            if retained.task_id != record.task_id:
                return
            # Remove the session first: interruption before the counter write
            # is a conservative overcount, never an unsafe undercount.
            self._state.remove_session(str(record.session_id))
            current = self._read_stored_unlocked(task_id)
            if current.session_count is not None and current.session_count > 0:
                self._persist_stored(
                    path,
                    current.model_copy(
                        update={"session_count": current.session_count - 1}
                    ),
                )

    def discard_implicit_task(self, task_id: str) -> None:
        """Roll back a *definitely failed* implicit create, never a live binding."""
        path = self._path(task_id)
        with self._store.transaction(path):
            self._read_stored_unlocked(task_id)
            if any(
                str(item.task_id or "") == task_id
                for item in self._state.snapshot_sessions().values()
            ):
                raise RuntimeError(
                    "cannot discard a Task with retained Sessions"
                )
            self._store.remove(path)

    async def list_tasks(self) -> list[TaskOutput]:
        """Return retained semantic tasks owned by the current principal."""
        return await asyncio.to_thread(self._list_tasks_sync)

    async def read_task(self, task_id: str) -> TaskOutput:
        return await asyncio.to_thread(
            lambda: self._task_output(self._read_stored_unlocked(task_id))
        )

    def admit_session_attachment(self, record: ControlSessionRecord) -> None:
        """Validate the task and publish the session under the task lock."""
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
            self._persist_stored(
                self._path(task_id),
                task.model_copy(
                    update={
                        "session_count": (
                            task.session_count + 1
                            if task.session_count is not None
                            else None
                        )
                    }
                ),
            )
            # If publication is interrupted, keep the conservative overcount:
            # a registry write may have reached disk before reporting failure.
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
                document.progress.blockers = [
                    item
                    for item in document.progress.blockers
                    if item
                    != "Automatically blocked after confirmed inactivity."
                ]
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
            if any(
                str(session.task_id or "") == task_id
                and session.task_created
                and session.status == "creating"
                for session in self._state.snapshot_sessions().values()
            ):
                raise ValueError(
                    "cannot delete automatically created Task while its Session is creating"
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
        todos: list[Any],
    ) -> TaskOutput:
        normalized = self._normalize_todos(todos)

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
        todos: list[Any],
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
        todos: list[Any],
    ) -> tuple[WriteTodosOutput, TaskOutput]:
        task = await self._write_task_output(task_id, todos)
        return self._todo_output(task, write=True), task  # type: ignore[return-value]
