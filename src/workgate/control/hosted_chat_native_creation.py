"""Bind durable Chat reservations to the native Control resource owners."""

import asyncio
import copy
import time
from typing import Any

from ..audit import (
    audit_call_context,
    audit_tool_call_end,
    audit_tool_call_start,
    new_audit_call_id,
)
from ..errors import PublicToolValueError
from ..hosted_chat.durable_tasks import DurableTaskRepository, Intent
from ..hosted_chat.generation_actor_lease import ActorLease
from ..hosted_chat.provider_common import ModelToolCall, complete_tool_batch
from ..oauth.core.context import current_oauth_claims
from ..oauth.core.scopes import scope_set
from ..schemas.result_models.session import SessionStartOutput
from .execution_context import control_execution_context
from .runtime import ControlRuntime
from .tool_timeouts import tool_timeout_s


class NativeCreationUncertain(RuntimeError):
    """The original native mutation has no confirmed receipt; never re-offer."""


def _grant(
    runtime: ControlRuntime, owner: str, scopes: set[str], expires: int | None
):
    if expires is not None and type(expires) is not int:
        raise PermissionError("Invalid native grant expiration")
    claims = current_oauth_claims()
    if claims is None:
        if runtime.config.auth_mode != "none" or owner != "local-user":
            raise PermissionError("Authenticated native principal required")
        return scopes, expires
    subject = str(claims.get("sub") or "").strip()
    if not subject or subject != owner:
        raise PermissionError(
            "Native principal does not match generation owner"
        )
    deadline = claims.get("exp")
    if type(deadline) is not int:
        raise PermissionError("Native grant expiration is required")
    return scopes.intersection(scope_set(str(claims.get("scope") or ""))), min(
        deadline, expires
    ) if expires is not None else deadline


def _registered(runtime: ControlRuntime) -> tuple[str, ...]:
    return tuple(
        key
        for key, record in runtime.control_state.snapshot_executors().items()
        if record.revoked_at is None
    )


async def _native_result(
    runtime: ControlRuntime,
    intent: Intent,
    call: ModelToolCall,
    *,
    dispatch: bool,
) -> tuple[str, dict]:
    arguments = call.arguments
    if call.name == "task":
        if dispatch:
            result = await runtime.task_service.create_task(
                label=arguments.get("label"),
                objective=arguments.get("objective"),
                reserved_task_id=intent.key,
            )
        else:
            try:
                result = await runtime.task_service.read_task(intent.key)
            except PublicToolValueError:
                raise NativeCreationUncertain(
                    "Native Task creation is unconfirmed"
                ) from None
        return result.task_id, result.model_dump(mode="json")

    task_id = arguments["task_id"]
    if dispatch:
        payload = await runtime.session_coordinator.start_session(
            workdir=arguments.get("workdir"),
            label=arguments.get("label"),
            executor_id=intent.machine,
            task_id=task_id,
            reserved_session_id=intent.key,
        )
        result = SessionStartOutput.model_validate(payload)
        if (
            result.session_id != intent.key
            or result.executor_id != intent.machine
            or result.task_id != task_id
        ):
            raise NativeCreationUncertain(
                "Native Session response has a mismatched identity"
            )
        return result.session_id, result.model_dump(mode="json")

    record = await runtime.session_coordinator.lookup_reserved_session(
        intent.key,
        executor_id=intent.machine,
        task_id=task_id,
    )
    if record is None:
        raise NativeCreationUncertain("Native Session creation is unconfirmed")
    # A lookup proves creation but does not contain the original orientation.
    # Do not invent git, environment, or capability data absent from the receipt.
    return str(record.session_id), {
        "session_id": record.session_id,
        "task_id": record.task_id,
        "executor_id": record.executor_id,
        "workdir": record.workdir,
        "recovered": True,
        "message": "Existing Session creation confirmed by native lookup; reuse this session_id.",
    }


async def create_native(
    runtime: ControlRuntime,
    repository: DurableTaskRepository,
    lease: ActorLease,
    call: ModelToolCall,
    *,
    machine: str,
    scopes: set[str],
    expires: int | None,
) -> dict[str, Any]:
    """Create once or recover a reserved Task/Session using the authenticated principal."""
    if repository.database.root != runtime.config.state_dir / "hosted-chat":
        raise PermissionError(
            "Native intent belongs to another Control runtime"
        )
    scopes, expires = _grant(runtime, lease.owner, scopes, expires)
    call = copy.deepcopy(call)
    complete_tool_batch([call])
    for key in ("label", "objective", "workdir"):
        value = call.arguments.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError("Invalid native creation arguments")
    intent = repository.reserve(
        lease,
        call,
        machine=machine,
        scopes=scopes,
        expires=expires,
        registered=_registered(runtime),
    )
    with control_execution_context(
        config=runtime.config,
        state_store=runtime.state_store,
        oauth_state=runtime.oauth_state,
        managed_jobs_runtime=runtime.managed_jobs_runtime,
    ):
        audit_id = new_audit_call_id()
        start = time.monotonic()
        sessions, tasks = audit_tool_call_start(
            call_id=audit_id,
            transport="hosted-chat",
            tool=call.name,
            input=call.arguments,
        )
        try:
            dispatch = repository.claim(
                lease,
                intent,
                call,
                scopes=scopes,
                expires=expires,
                registered=_registered(runtime),
            )
            with audit_call_context(audit_id, sessions, tasks):
                native_id, result = await asyncio.wait_for(
                    _native_result(runtime, intent, call, dispatch=dispatch),
                    timeout=tool_timeout_s(call.name, config=runtime.config),
                )
            confirmed = repository.bind(lease, intent, native_id)
            if confirmed.state != "active":
                raise PermissionError("Native creation was cancelled")
            # The receipt is durable even when access was revoked in flight.
            # Recheck the current authorization before returning it to the model.
            repository.reserve(
                lease,
                call,
                machine=machine,
                scopes=scopes,
                expires=expires,
                registered=_registered(runtime),
            )
        except BaseException as exc:
            audit_tool_call_end(
                call_id=audit_id,
                transport="hosted-chat",
                tool=call.name,
                ok=False,
                duration_ms=int((time.monotonic() - start) * 1000),
                error={"type": type(exc).__name__},
                session_ids=sessions,
                task_ids=tasks,
            )
            raise
        audit_tool_call_end(
            call_id=audit_id,
            transport="hosted-chat",
            tool=call.name,
            ok=True,
            duration_ms=int((time.monotonic() - start) * 1000),
            output=result,
            session_ids=sessions,
            task_ids=tasks,
        )
        return result
