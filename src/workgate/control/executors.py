"""Canonical executor fleet discovery and administration."""

import time

from .. import __version__
from ..audit import audit
from ..config.control import ControlConfig
from ..schemas.result_models.executor import (
    ExecutorBootstrapRecipe,
    ExecutorFleetEntry,
    ExecutorFleetOutput,
    ExecutorSessionCapacity,
)
from .executor_transport import ExecutorTransport
from .pairing import ExecutorPairingService
from .sessions import ControlSessionCoordinator
from .state import ControlState, ExecutorTrustRecord


class ExecutorNotFoundError(ValueError):
    """Raised when fleet administration targets an unknown executor."""


class ControlExecutorFleetService:
    """Compose canonical executor trust, presence, admission, and admin state."""

    def __init__(
        self,
        state: ControlState,
        transport: ExecutorTransport,
        sessions: ControlSessionCoordinator,
        pairing: ExecutorPairingService,
        config: ControlConfig,
    ) -> None:
        self._state = state
        self._transport = transport
        self._sessions = sessions
        self._pairing = pairing
        self._config = config

    def _bootstrap(self) -> ExecutorBootstrapRecipe:
        url = (
            self._config.resolved_base_url.rstrip("/")
            + "/executor/v1/bootstrap"
        )
        return ExecutorBootstrapRecipe(
            url=url,
            command=f"curl -fsSL {url} | bash -s -- --persist",
        )

    def _capacity(self) -> ExecutorSessionCapacity:
        used, limit, available = self._sessions.session_capacity()
        return ExecutorSessionCapacity(
            used=used,
            limit=limit,
            available=available,
        )

    async def _entry(self, record: ExecutorTrustRecord) -> ExecutorFleetEntry:
        executor_id = str(record.executor_id)
        inventory = await self._transport.inventory(executor_id)
        online = (
            False
            if record.revoked_at is not None
            else await self._transport.is_online(executor_id)
        )
        queued, offered, command_limit = await self._transport.command_status(
            executor_id
        )
        eligible, reasons = await self._sessions.executor_session_eligibility(
            executor_id
        )
        capacity = self._capacity()
        admission_reasons = list(reasons)
        if eligible and not capacity.available:
            eligible = False
            admission_reasons.append("session_capacity_full")
        runtime = None if inventory is None else inventory.runtime
        runtime_update_required = (
            runtime is not None and runtime.workgate_version != __version__
        )
        return ExecutorFleetEntry(
            executor_id=executor_id,
            name=record.name,
            created_at=record.created_at,
            trusted=record.revoked_at is None,
            revoked_at=record.revoked_at,
            draining=record.draining,
            online=online,
            last_seen_at=await self._transport.last_seen_at(executor_id),
            runtime=runtime,
            required_workgate_version=__version__,
            runtime_update_required=runtime_update_required,
            capabilities=[]
            if inventory is None
            else list(inventory.capabilities),
            active_sessions=self._sessions.executor_active_session_count(
                executor_id
            ),
            queued_commands=queued,
            offered_commands=offered,
            command_limit=command_limit,
            session_admission=eligible,
            session_admission_reasons=admission_reasons,
        )

    def _record(self, executor_id: str) -> ExecutorTrustRecord:
        record = self._state.snapshot_executors().get(executor_id)
        if record is None:
            raise ExecutorNotFoundError(f"unknown executor_id {executor_id!r}")
        return record

    @staticmethod
    def _require_trusted(record: ExecutorTrustRecord) -> None:
        if record.revoked_at is not None:
            raise ValueError(f"executor {record.executor_id!r} is revoked")

    @staticmethod
    def _require_executor_id(action: str, executor_id: str | None) -> str:
        if executor_id is None:
            raise ValueError(f"executor_id is required for action={action}")
        return executor_id

    async def list(self) -> ExecutorFleetOutput:
        records = sorted(
            self._state.snapshot_executors().values(),
            key=lambda record: (
                record.name.casefold(),
                str(record.executor_id),
            ),
        )
        entries = [await self._entry(record) for record in records]
        capacity = self._capacity()
        output = ExecutorFleetOutput(
            action="list",
            executors=entries,
            session_capacity=capacity,
        )
        if capacity.available and not any(
            entry.session_admission for entry in entries
        ):
            output.bootstrap = self._bootstrap()
        return output

    async def inspect(self, executor_id: str) -> ExecutorFleetOutput:
        return ExecutorFleetOutput(
            action="inspect",
            executor=await self._entry(self._record(executor_id)),
            session_capacity=self._capacity(),
        )

    async def rename(self, executor_id: str, name: str) -> ExecutorFleetOutput:
        self._record(executor_id)
        record = await self._transport.rename_executor(executor_id, name=name)
        audit("executor_renamed", executor_id=executor_id, name=record.name)
        return ExecutorFleetOutput(
            action="rename",
            executor=await self._entry(record),
            session_capacity=self._capacity(),
        )

    async def reset(self, executor_id: str) -> ExecutorFleetOutput:
        record = self._record(executor_id)
        self._require_trusted(record)
        cancelled, preserved = await self._transport.reset_queued(executor_id)
        audit(
            "executor_reset",
            executor_id=executor_id,
            cancelled_queued=cancelled,
            preserved_offered=preserved,
        )
        return ExecutorFleetOutput(
            action="reset",
            executor=await self._entry(self._record(executor_id)),
            session_capacity=self._capacity(),
            cancelled_queued=cancelled,
            preserved_offered=preserved,
        )

    async def set_draining(
        self, executor_id: str, *, draining: bool
    ) -> ExecutorFleetOutput:
        current = self._record(executor_id)
        self._require_trusted(current)
        record = await self._sessions.set_executor_draining(
            executor_id, draining=draining
        )
        audit(
            "executor_drain_changed",
            executor_id=executor_id,
            draining=draining,
        )
        return ExecutorFleetOutput(
            action="drain" if draining else "resume",
            executor=await self._entry(record),
            session_capacity=self._capacity(),
        )

    async def revoke(self, executor_id: str) -> ExecutorFleetOutput:
        self._record(executor_id)
        record = await self._transport.revoke_executor(
            executor_id, revoked_at=time.time()
        )
        await self._pairing.clear_executor_delivery(executor_id)
        audit("executor_revoked", executor_id=executor_id)
        return ExecutorFleetOutput(
            action="revoke",
            executor=await self._entry(record),
            session_capacity=self._capacity(),
        )

    async def execute(
        self,
        *,
        action: str = "list",
        executor_id: str | None = None,
        name: str | None = None,
    ) -> ExecutorFleetOutput:
        """Execute one bounded fleet action."""
        if action == "list":
            if executor_id is not None or name is not None:
                raise ValueError("list does not accept executor_id or name")
            return await self.list()
        if action == "bootstrap":
            if executor_id is not None or name is not None:
                raise ValueError(
                    "bootstrap does not accept executor_id or name"
                )
            return ExecutorFleetOutput(
                action="bootstrap",
                session_capacity=self._capacity(),
                bootstrap=self._bootstrap(),
            )

        target = self._require_executor_id(action, executor_id)
        if action == "inspect":
            if name is not None:
                raise ValueError("inspect does not accept name")
            return await self.inspect(target)
        if action == "rename":
            if name is None:
                raise ValueError("name is required for action=rename")
            return await self.rename(target, name)
        if name is not None:
            raise ValueError(f"{action} does not accept name")
        if action == "reset":
            return await self.reset(target)
        if action == "drain":
            return await self.set_draining(target, draining=True)
        if action == "resume":
            return await self.set_draining(target, draining=False)
        if action == "revoke":
            return await self.revoke(target)
        raise ValueError(f"unsupported executor action: {action}")
