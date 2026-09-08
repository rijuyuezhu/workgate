"""Control authority for final shared executor sessions."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Literal

from pydantic import JsonValue

from ..errors import exception_from_tool_error
from ..protocol.executor import (
    EXECUTOR_CAPABILITY_SESSIONS,
    SESSION_CHANGE_CWD_OP,
    SESSION_CREATE_OP,
    SESSION_LOOKUP_OP,
    SESSION_TERMINATE_OP,
    ExecutorResult,
    SessionInventorySummary,
)
from ..protocol.ids import new_session_id
from .executor_transport import (
    ExecutorTransport,
    ExecutorTransportError,
    abandoned_command_was_offered,
)
from .state import ControlSessionRecord, ControlState

_CREATE_ABSENT = "session_create_absent"
_CREATE_UNCONFIRMED = "session_create_unconfirmed"
_LOOKUP_TIMEOUT_S = 2.0
_ACTIVE_SESSION_WINDOW_S = 5 * 60 * 60

logger = logging.getLogger(__name__)

SessionAvailability = Literal[
    "creating",
    "available",
    "executor_offline",
    "missing_on_executor",
    "terminating",
    "ended",
]


class ControlSessionCoordinator:
    """Linearize session lifecycle intent with executor command admission."""

    def __init__(
        self,
        state: ControlState,
        transport: ExecutorTransport,
        *,
        max_agent_sessions: int | None = None,
        agent_session_retention_s: int = 0,
        clock=time.time,
    ) -> None:
        self._state = state
        self._transport = transport
        self._max_agent_sessions = max_agent_sessions
        self._agent_session_retention_s = max(0, agent_session_retention_s)
        self._clock = clock
        self._locks: dict[str, asyncio.Lock] = {}
        self._capacity_lock = asyncio.Lock()
        self._reconcile_tasks: set[asyncio.Task[None]] = set()
        self._missing_by_executor: dict[str, dict[str, float]] = {}
        self._activity_by_session: dict[str, float] = {}
        self._auto_cleanup_blocked: Callable[[str], Awaitable[bool]] | None = (
            None
        )
        self._before_terminate: Callable[[str], Awaitable[list[str]]] | None = (
            None
        )

    def set_control_resource_hooks(
        self,
        *,
        auto_cleanup_blocked: Callable[[str], Awaitable[bool]],
        before_terminate: Callable[[str], Awaitable[list[str]]],
    ) -> None:
        """Attach control-owned resource protection to the shared-session lifecycle."""
        self._auto_cleanup_blocked = auto_cleanup_blocked
        self._before_terminate = before_terminate

    async def aclose(self) -> None:
        tasks = tuple(self._reconcile_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reconcile_tasks.clear()
        self._missing_by_executor.clear()
        self._activity_by_session.clear()
        self._locks.clear()

    async def select_executor(self, executor_id: str | None = None) -> str:
        """Choose only an online, trusted, protocol-compatible session executor."""
        candidates: list[str] = []
        for record in self._state.snapshot_executors().values():
            candidate = str(record.executor_id)
            if record.revoked_at is not None:
                continue
            if executor_id is not None and candidate != executor_id:
                continue
            if not await self._transport.is_online(candidate):
                continue
            hello = await self._transport.inventory(candidate)
            if (
                hello is None
                or EXECUTOR_CAPABILITY_SESSIONS not in hello.capabilities
            ):
                continue
            candidates.append(candidate)
        if executor_id is not None:
            if candidates == [executor_id]:
                return executor_id
            raise RuntimeError(
                f"executor {executor_id!r} is not currently eligible for sessions"
            )
        if len(candidates) != 1:
            raise RuntimeError(
                "session_start requires exactly one eligible executor or an explicit executor_id"
            )
        return candidates[0]

    async def start_session(
        self,
        *,
        workdir: str,
        label: str | None = None,
        executor_id: str | None = None,
    ) -> JsonValue:
        selected = await self.select_executor(executor_id)
        session_id = str(new_session_id())
        async with self._capacity_lock:
            await self._reap_for_capacity()
            self._require_capacity()
            now = self._clock()
            record = ControlSessionRecord(
                session_id=session_id,
                executor_id=selected,
                requested_workdir=workdir,
                resolved_workdir_display=None,
                label=label,
                status="creating",
                created_at=now,
                updated_at=now,
            )
            self._state.put_session(record)
        lock = self._lock(session_id)
        async with lock:
            try:
                result = await self._transport.call(
                    selected,
                    SESSION_CREATE_OP,
                    {"workdir": workdir, "label": label},
                    session_id=session_id,
                )
            except (asyncio.CancelledError, TimeoutError) as exc:
                if not abandoned_command_was_offered(exc):
                    self._state.remove_session(session_id)
                else:
                    await self._reconcile_ambiguous_create(record)
                raise
            except ExecutorTransportError as exc:
                if not abandoned_command_was_offered(exc):
                    self._state.remove_session(session_id)
                else:
                    await self._reconcile_ambiguous_create(record)
                raise
            return self._finish_create(record, result)

    async def call_session_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> JsonValue:
        session_id = self._required_session_id(args)
        lock = self._lock(session_id)
        async with lock:
            record = self._require_status(session_id, {"active"})
            await self._require_available(record)
            wire_args = {
                key: value for key, value in args.items() if key != "session_id"
            }
            result = await self._transport.call(
                str(record.executor_id),
                tool_name,
                wire_args,
                session_id=session_id,
            )
            self.observe_session_activity(session_id)
            payload = self._unwrap(result)
            return payload

    async def change_cwd(self, session_id: str, workdir: str) -> JsonValue:
        lock = self._lock(session_id)
        async with lock:
            record = self._require_status(session_id, {"active"})
            await self._require_available(record)
            result = await self._transport.call(
                str(record.executor_id),
                SESSION_CHANGE_CWD_OP,
                {"workdir": workdir},
                session_id=session_id,
            )
            self.observe_session_activity(session_id)
            payload = self._unwrap(result)
            resolved = self._resolved_workdir(payload)
            now = self._clock()
            self._state.put_session(
                record.model_copy(
                    update={
                        "requested_workdir": workdir,
                        "resolved_workdir_display": resolved,
                        "updated_at": now,
                    }
                )
            )
            self.observe_session_activity(session_id, observed_at=now)
            return self._with_executor_binding(record, payload)

    async def end_session(
        self, session_id: str, *, force: bool = False
    ) -> dict[str, Any]:
        lock = self._lock(session_id)
        async with lock:
            record = self._require_status(
                session_id, {"creating", "active", "terminating"}
            )
            return await self._end_record_locked(record, force=force)

    async def _end_record_locked(
        self, record: ControlSessionRecord, *, force: bool
    ) -> dict[str, Any]:
        session_id = str(record.session_id)
        if record.status != "terminating":
            record = record.model_copy(
                update={
                    "status": "terminating",
                    "updated_at": self._clock(),
                }
            )
            self._state.put_session(record)
        control_stopped_jobs: list[str] = []
        if self._before_terminate is not None:
            control_stopped_jobs = await self._before_terminate(session_id)
        try:
            result = await self._transport.call(
                str(record.executor_id),
                SESSION_TERMINATE_OP,
                {},
                session_id=session_id,
            )
        except ExecutorTransportError:
            if not force:
                raise
            self._mark_ended(record)
            return {
                "session_id": session_id,
                "executor_id": str(record.executor_id),
                "ended": True,
                "force_released": True,
                "stopped_jobs": control_stopped_jobs,
            }
        payload = self._unwrap(result)
        if not isinstance(payload, dict) or payload.get("absent") is not True:
            raise RuntimeError("executor did not confirm session absence")
        self._mark_ended(record)
        executor_stopped_jobs = payload.get("stopped_jobs", [])
        stopped_jobs = list(
            dict.fromkeys(
                [*control_stopped_jobs, *executor_stopped_jobs]
                if isinstance(executor_stopped_jobs, list)
                else control_stopped_jobs
            )
        )
        return {
            "session_id": session_id,
            "executor_id": str(record.executor_id),
            "ended": True,
            "force_released": False,
            "stopped_jobs": stopped_jobs,
            "stopped_shells": payload.get("stopped_shells", []),
        }

    async def _reap_for_capacity(self) -> None:
        maximum = self._max_agent_sessions
        if maximum is None:
            return
        now = self._clock()
        if self._agent_session_retention_s > 0:
            cutoff = now - self._agent_session_retention_s
            for record in self._cleanup_records():
                await self._auto_end_if_eligible(record, cutoff=cutoff)

        if self._nonended_session_count() < maximum:
            return
        overflow_cutoff = now - _ACTIVE_SESSION_WINDOW_S
        candidates: list[tuple[float, ControlSessionRecord]] = []
        for record in self._cleanup_records():
            observed_activity = await self._cleanup_activity_before_cutoff(
                record, cutoff=overflow_cutoff
            )
            if observed_activity is None:
                continue
            candidates.append((observed_activity, record))
        candidates.sort(key=lambda item: (item[0], item[1].created_at))
        for observed_activity, record in candidates:
            if self._nonended_session_count() < maximum:
                break
            await self._auto_end_if_eligible(
                record,
                cutoff=overflow_cutoff,
                expected_last_active_at=observed_activity,
            )

    async def _auto_end_if_eligible(
        self,
        record: ControlSessionRecord,
        *,
        cutoff: float,
        expected_last_active_at: float | None = None,
    ) -> bool:
        session_id = str(record.session_id)
        async with self._lock(session_id):
            current = self._state.snapshot_sessions().get(session_id)
            if (
                current is None
                or current.status not in {"creating", "active"}
                or current.executor_id != record.executor_id
            ):
                return False
            if (
                self._auto_cleanup_blocked is not None
                and await self._auto_cleanup_blocked(session_id)
            ):
                return False
            observed_activity = await self._cleanup_activity_before_cutoff(
                current, cutoff=cutoff
            )
            if observed_activity is None:
                return False
            if (
                expected_last_active_at is not None
                and observed_activity != expected_last_active_at
            ):
                return False
            await self._end_record_locked(current, force=False)
            return True

    async def _cleanup_activity_before_cutoff(
        self, record: ControlSessionRecord, *, cutoff: float
    ) -> float | None:
        """Return authoritative cleanup age without guessing executor activity."""
        if record.status == "creating":
            return record.updated_at if record.updated_at < cutoff else None
        if record.status != "active":
            return None
        executor_id = str(record.executor_id)
        missing_since = self._missing_by_executor.get(executor_id, {}).get(
            str(record.session_id)
        )
        if missing_since is not None:
            if not await self._transport.is_online(executor_id):
                return None
            return missing_since if missing_since < cutoff else None
        summary = await self._lookup_cleanup_summary(record)
        if not self._cleanup_eligible(summary, cutoff=cutoff):
            return None
        assert summary is not None and summary.last_active_at is not None
        return summary.last_active_at

    async def _lookup_cleanup_summary(
        self, record: ControlSessionRecord
    ) -> SessionInventorySummary | None:
        executor_id = str(record.executor_id)
        if not await self._transport.is_online(executor_id):
            return None
        try:
            result = await self._transport.call(
                executor_id,
                SESSION_LOOKUP_OP,
                {},
                session_id=str(record.session_id),
                timeout_s=_LOOKUP_TIMEOUT_S,
            )
        except Exception:
            return None
        if not result.ok:
            return None
        if result.result is None:
            missing = self._missing_by_executor.setdefault(executor_id, {})
            missing.setdefault(str(record.session_id), float(self._clock()))
            return None
        try:
            summary = SessionInventorySummary.model_validate(result.result)
        except Exception:
            return None
        missing = self._missing_by_executor.get(executor_id)
        if missing is not None:
            missing.pop(str(record.session_id), None)
            if not missing:
                self._missing_by_executor.pop(executor_id, None)
        if summary.last_active_at is not None:
            self.observe_session_activity(
                str(record.session_id), observed_at=summary.last_active_at
            )
        return summary

    @staticmethod
    def _cleanup_eligible(
        summary: SessionInventorySummary | None, *, cutoff: float
    ) -> bool:
        return bool(
            summary is not None
            and summary.last_active_at is not None
            and summary.last_active_at < cutoff
            and not summary.has_persistent_shells
            and not summary.has_active_jobs
        )

    def _cleanup_records(self) -> tuple[ControlSessionRecord, ...]:
        return tuple(
            record
            for record in self._state.snapshot_sessions().values()
            if record.status in {"creating", "active"}
        )

    def _nonended_session_count(self) -> int:
        return sum(
            record.status != "ended"
            for record in self._state.snapshot_sessions().values()
        )

    def _require_capacity(self) -> None:
        maximum = self._max_agent_sessions
        if maximum is not None and self._nonended_session_count() >= maximum:
            raise RuntimeError(
                "agent session limit reached: "
                f"{maximum}; end an active session or wait for retention cleanup"
            )

    async def reconcile_hello(self, executor_id: str) -> None:
        """Reconcile only against one complete authenticated executor inventory."""
        hello = await self._transport.inventory(executor_id)
        if (
            hello is None
            or EXECUTOR_CAPABILITY_SESSIONS not in hello.capabilities
        ):
            return
        reported = {str(item.session_id): item for item in hello.sessions}
        all_records = self._state.snapshot_sessions()
        observed_at = float(self._clock())
        previous_missing = self._missing_by_executor.get(executor_id, {})
        bound = {
            session_id: record
            for session_id, record in all_records.items()
            if str(record.executor_id) == executor_id
            and record.status != "ended"
        }
        missing_on_executor: dict[str, float] = {}
        for session_id, record in bound.items():
            item = reported.get(session_id)
            if record.status == "terminating":
                if item is None:
                    self._mark_ended(record)
                else:
                    self._schedule_termination(record)
                continue
            if item is not None:
                if item.last_active_at is not None:
                    self.observe_session_activity(
                        session_id, observed_at=item.last_active_at
                    )
                if record.status == "creating" or (
                    record.resolved_workdir_display != item.resolved_workdir
                ):
                    self._state.put_session(
                        record.model_copy(
                            update={
                                "status": "active",
                                "resolved_workdir_display": item.resolved_workdir,
                                "updated_at": self._clock(),
                            }
                        )
                    )
                continue
            if record.status == "active":
                previous_observation = previous_missing.get(session_id)
                missing_on_executor[session_id] = (
                    observed_at
                    if previous_observation is None
                    else previous_observation
                )
        if missing_on_executor:
            self._missing_by_executor[executor_id] = missing_on_executor
        else:
            self._missing_by_executor.pop(executor_id, None)

        for session_id in reported:
            record = all_records.get(session_id)
            if record is None:
                logger.warning(
                    "executor %s reported orphan session %s unknown to control",
                    executor_id,
                    session_id,
                )
            elif str(record.executor_id) != executor_id:
                logger.warning(
                    "executor %s reported session %s bound to executor %s",
                    executor_id,
                    session_id,
                    record.executor_id,
                )

    @asynccontextmanager
    async def session_admission(self, session_ids: tuple[str, ...]):
        """Hold stable-order lifecycle locks for one single/multi-session operation."""
        unique = tuple(dict.fromkeys(session_ids))
        async with AsyncExitStack() as stack:
            for session_id in sorted(unique):
                await stack.enter_async_context(self._lock(session_id))
            records = tuple(
                self._require_status(session_id, {"active"})
                for session_id in unique
            )
            for record in records:
                await self._require_available(record)
            yield records

    async def admit_sessions(
        self, session_ids: tuple[str, ...]
    ) -> tuple[ControlSessionRecord, ...]:
        """Validate multi-session active admission without retaining the lease."""
        async with self.session_admission(session_ids) as records:
            return records

    def require_session_status(
        self, session_id: str, allowed: set[str]
    ) -> ControlSessionRecord:
        """Resolve one durable public session record for control-owned operations."""
        return self._require_status(session_id, allowed)

    async def session_availability(
        self, session_id: str
    ) -> SessionAvailability:
        """Project executor availability without mutating durable session lifecycle."""
        record = self._state.snapshot_sessions().get(session_id)
        if record is None:
            raise ValueError(
                f"unknown session_id {session_id!r}; call session_start first"
            )
        if record.status != "active":
            return record.status
        executor_id = str(record.executor_id)
        if not await self._transport.is_online(executor_id):
            return "executor_offline"
        if session_id in self._missing_by_executor.get(executor_id, {}):
            return "missing_on_executor"
        return "available"

    async def session_activity_projection(
        self, session_id: str
    ) -> tuple[SessionAvailability, float | None]:
        """Project availability plus process-local activity without executor RPC."""
        record = self._state.snapshot_sessions().get(session_id)
        if record is None:
            raise ValueError(
                f"unknown session_id {session_id!r}; call session_start first"
            )
        availability = await self.session_availability(session_id)
        return availability, self._activity_by_session.get(session_id)

    def observe_session_activity(
        self, session_id: str, *, observed_at: float | None = None
    ) -> None:
        """Refresh one process-local activity observation without durable writes."""
        value = float(self._clock() if observed_at is None else observed_at)
        previous = self._activity_by_session.get(session_id)
        if previous is None or value > previous:
            self._activity_by_session[session_id] = value

    def _finish_create(
        self, record: ControlSessionRecord, result: ExecutorResult
    ) -> JsonValue:
        if not result.ok:
            assert result.error is not None
            if result.error.code != _CREATE_UNCONFIRMED:
                self._state.remove_session(str(record.session_id))
            raise RuntimeError(
                f"executor session.create failed: {result.error.code}: {result.error.message}"
            )
        payload = self._unwrap(result)
        resolved = self._resolved_workdir(payload)
        now = self._clock()
        self._state.put_session(
            record.model_copy(
                update={
                    "status": "active",
                    "resolved_workdir_display": resolved,
                    "updated_at": now,
                }
            )
        )
        self.observe_session_activity(str(record.session_id), observed_at=now)
        return self._with_executor_binding(record, payload)

    async def _reconcile_ambiguous_create(
        self, record: ControlSessionRecord
    ) -> None:
        executor_id = str(record.executor_id)
        if not await self._transport.is_online(executor_id):
            return
        try:
            result = await self._transport.call(
                executor_id,
                SESSION_LOOKUP_OP,
                {},
                session_id=str(record.session_id),
                timeout_s=_LOOKUP_TIMEOUT_S,
            )
        except Exception:
            return
        if not result.ok or result.result is None:
            return
        try:
            item = SessionInventorySummary.model_validate(result.result)
        except Exception:
            return
        self._state.put_session(
            record.model_copy(
                update={
                    "status": "active",
                    "resolved_workdir_display": item.resolved_workdir,
                    "updated_at": self._clock(),
                }
            )
        )
        if item.last_active_at is not None:
            self.observe_session_activity(
                str(record.session_id), observed_at=item.last_active_at
            )

    def _schedule_termination(self, record: ControlSessionRecord) -> None:
        task = asyncio.create_task(
            self._continue_termination(record),
            name=f"workgate-session-reconcile-{record.session_id}",
        )
        self._reconcile_tasks.add(task)
        task.add_done_callback(self._reconcile_tasks.discard)

    async def _continue_termination(self, record: ControlSessionRecord) -> None:
        await asyncio.sleep(0)
        try:
            await self.end_session(str(record.session_id))
        except Exception:
            return

    def _mark_ended(self, record: ControlSessionRecord) -> None:
        self._activity_by_session.pop(str(record.session_id), None)
        missing = self._missing_by_executor.get(str(record.executor_id))
        if missing is not None:
            missing.pop(str(record.session_id), None)
            if not missing:
                self._missing_by_executor.pop(str(record.executor_id), None)
        self._state.put_session(
            record.model_copy(
                update={"status": "ended", "updated_at": self._clock()}
            )
        )

    async def _require_available(self, record: ControlSessionRecord) -> None:
        availability = await self.session_availability(str(record.session_id))
        if availability == "available":
            return
        if availability == "executor_offline":
            raise RuntimeError(
                f"session {record.session_id!r} executor is offline"
            )
        if availability == "missing_on_executor":
            raise RuntimeError(
                f"session {record.session_id!r} is missing on executor"
            )
        raise RuntimeError(
            f"session {record.session_id!r} is {availability}; executor work is unavailable"
        )

    def _require_status(
        self, session_id: str, allowed: set[str]
    ) -> ControlSessionRecord:
        record = self._state.snapshot_sessions().get(session_id)
        if record is None:
            raise ValueError(
                f"unknown session_id {session_id!r}; call session_start first"
            )
        if record.status not in allowed:
            raise ValueError(
                f"session {session_id!r} is {record.status}; operation requires {sorted(allowed)}"
            )
        return record

    def _lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    @staticmethod
    def _required_session_id(args: dict[str, Any]) -> str:
        value = args.get("session_id")
        if not isinstance(value, str) or not value:
            raise ValueError("machine-facing tool requires session_id")
        return value

    @staticmethod
    def _unwrap(result: ExecutorResult) -> JsonValue:
        if not result.ok:
            assert result.error is not None
            if result.error.data is not None:
                raise exception_from_tool_error(dict(result.error.data))
            raise RuntimeError(
                f"executor operation failed: {result.error.code}: {result.error.message}"
            )
        return result.result

    @staticmethod
    def _resolved_workdir(payload: JsonValue) -> str:
        if not isinstance(payload, dict):
            raise RuntimeError("executor session result is not an object")
        value = payload.get("workdir")
        if not isinstance(value, str) or not value:
            raise RuntimeError("executor session result is missing workdir")
        return value

    @staticmethod
    def _with_executor_binding(
        record: ControlSessionRecord, payload: JsonValue
    ) -> dict[str, JsonValue]:
        if not isinstance(payload, dict):
            raise RuntimeError("executor session result is not an object")
        return {**payload, "executor_id": str(record.executor_id)}
