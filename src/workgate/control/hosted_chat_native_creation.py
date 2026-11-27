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
from ..errors import PublicToolError, PublicToolValueError
from ..hosted_chat.durable_tasks import (
    DurableTaskRepository,
    Intent,
    ReservedCopyRequest,
    reserved_job_request,
    reserved_shell_request,
)
from ..hosted_chat.generation_actor_lease import ActorLease
from ..hosted_chat.provider_common import ModelToolCall, complete_tool_batch
from ..jobs.managed import (
    ReservedManagedJobRetryRejected,
    lookup_managed_job_attempt,
    managed_job_tail_execute,
)
from ..oauth.core.context import current_oauth_claims
from ..oauth.core.scopes import scope_set
from ..protocol.executor import (
    JOB_LOOKUP_ATTEMPT_OP,
    JOB_RETRY_RESERVED_OP,
    JOB_START_RESERVED_OP,
    SHELL_LOOKUP_RESERVED_OP,
    SHELL_START_RESERVED_OP,
    ReservedJobAttemptRequest,
    ReservedShellLookupResult,
)
from ..schemas.result_models.jobs import (
    JobOutput,
    JobRetryOutput,
    JobStartOutput,
    ReservedJobAttemptReceipt,
)
from ..schemas.result_models.session import SessionStartOutput
from ..schemas.result_models.shell import (
    RunPythonCodeOutput,
    ShellExecutionOutput,
)
from .execution_context import control_execution_context
from .runtime import ControlRuntime
from .session_copy import SESSION_COPY_MANAGED_KIND
from .tool_timeouts import tool_timeout_s


class NativeCreationUncertain(RuntimeError):
    """The original native mutation has no confirmed receipt; never re-offer."""


class NativeRetryRejected(RuntimeError):
    """The native owner explicitly refused retry before starting an attempt."""


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


async def _native_job_result(
    runtime: ControlRuntime,
    intent: Intent,
    call: ModelToolCall,
    *,
    dispatch: bool,
) -> tuple[str, dict]:
    session_id = intent.session_id
    if session_id is None:
        raise PermissionError("Missing native Job Session reservation")
    coordinator = runtime.session_coordinator
    record = coordinator.require_session_status(session_id, {"active"})
    if record.executor_id != intent.machine:
        raise PermissionError("Native Job Session binding mismatch")
    if dispatch:
        request = reserved_job_request(call, intent.key)
        payload = await coordinator.call_session_tool(
            JOB_START_RESERVED_OP,
            request.model_dump(mode="json") | {"session_id": session_id},
        )
        model = (
            ShellExecutionOutput if call.name == "bash" else RunPythonCodeOutput
        )
        result = model.model_validate(payload)
        if (
            result.mode != "job"
            or result.result.get("job_id") != intent.key
            or result.result.get("session_id") != session_id
            or result.result.get("attempts") != 1
        ):
            raise NativeCreationUncertain(
                "Native Job response has a mismatched identity"
            )
        return intent.key, result.model_dump(mode="json")

    receipt = await _job_attempt_receipt(
        runtime,
        intent,
        ReservedJobAttemptRequest(job_id=intent.key, attempt=1),
        dispatch=False,
    )
    assert receipt.job is not None
    return intent.key, {
        "mode": "job",
        "command": receipt.job.command,
        "cwd": receipt.job.cwd,
        "result": receipt.job.model_dump(mode="json")
        | {"stdout": receipt.output or ""},
        "recovered": True,
        "message": "Existing Job creation confirmed by exact native attempt lookup; reuse this job_id.",
    }


async def _job_attempt_receipt(
    runtime: ControlRuntime,
    intent: Intent,
    request: ReservedJobAttemptRequest,
    *,
    dispatch: bool,
) -> ReservedJobAttemptReceipt:
    session_id = intent.session_id
    if session_id is None:
        raise PermissionError("Missing native Job Session reservation")
    coordinator = runtime.session_coordinator
    record = coordinator.require_session_status(session_id, {"active"})
    if record.executor_id != intent.machine:
        raise PermissionError("Native Job Session binding mismatch")
    try:
        receipt = ReservedJobAttemptReceipt.model_validate(
            await coordinator.call_session_tool(
                JOB_RETRY_RESERVED_OP if dispatch else JOB_LOOKUP_ATTEMPT_OP,
                request.model_dump(mode="json") | {"session_id": session_id},
            )
        )
    except PublicToolError, RuntimeError, ValueError, KeyError, TimeoutError:
        if dispatch:
            raise
        raise NativeCreationUncertain(
            "Native Job attempt is unconfirmed"
        ) from None
    if (
        dispatch
        and receipt.matches(request, session_id)
        and receipt.state == "rejected"
    ):
        raise NativeRetryRejected(
            "Native Job retry was not admitted; inspect the current Job before submitting another retry"
        )
    if (
        not receipt.matches(request, session_id)
        or receipt.state != "matching"
        or receipt.job is None
    ):
        raise NativeCreationUncertain(
            "Native Job attempt is unconfirmed or superseded"
        )
    return receipt


async def _managed_retry_job(
    runtime: ControlRuntime,
    intent: Intent,
    request: ReservedJobAttemptRequest,
    *,
    dispatch: bool,
) -> JobRetryOutput:
    source, destination = intent.session_id, intent.destination_session_id
    if (
        source is None
        or destination is None
        or intent.destination_machine is None
    ):
        raise PermissionError("Missing managed copy retry binding")
    expected_executors = {
        source: intent.machine,
        destination: intent.destination_machine,
    }
    if dispatch:
        try:
            result = await runtime.session_copy_service.retry_reserved(
                request,
                source=source,
                destination=destination,
                expected_executors=expected_executors,
            )
        except ReservedManagedJobRetryRejected as exc:
            raise NativeRetryRejected(
                "Native managed Job retry was not admitted"
            ) from exc
        if (
            result.job_id != request.job_id
            or result.session_id != source
            or result.kind != "managed"
            or result.attempts != request.attempt
        ):
            raise NativeCreationUncertain("Managed Job retry receipt mismatch")
        return result
    async with runtime.session_coordinator.session_admission(
        tuple(expected_executors),
        require_available=(),
    ) as records:
        if expected_executors != {
            str(record.session_id): str(record.executor_id)
            for record in records
        }:
            raise PermissionError("Managed copy Session bindings changed")
        try:
            state, job = lookup_managed_job_attempt(
                source,
                request,
                expected_kind=SESSION_COPY_MANAGED_KIND,
                expected_payload={
                    "src_session_id": source,
                    "dst_session_id": destination,
                },
            )
        except RuntimeError, ValueError, KeyError, TimeoutError:
            raise NativeCreationUncertain(
                "Native managed Job attempt is unconfirmed"
            ) from None
    if state != "matching" or job is None:
        raise NativeCreationUncertain(
            "Native managed Job attempt is unconfirmed or superseded"
        )
    return JobRetryOutput(**job.model_dump())


async def _native_retry_result(
    runtime: ControlRuntime,
    intent: Intent,
    *,
    dispatch: bool,
) -> tuple[str, dict]:
    if intent.target_job_id is None or intent.attempt is None:
        raise PermissionError("Missing native Job retry reservation")
    request = ReservedJobAttemptRequest(
        job_id=intent.target_job_id,
        attempt=intent.attempt,
        retry_key=intent.key,
    )
    if intent.destination_session_id is not None:
        job = await _managed_retry_job(
            runtime, intent, request, dispatch=dispatch
        )
    else:
        receipt = await _job_attempt_receipt(
            runtime, intent, request, dispatch=dispatch
        )
        assert receipt.job is not None
        job = JobRetryOutput(**receipt.job.model_dump())
    result = JobOutput(operation="retry", retried=[job])
    return intent.target_job_id, result.model_dump(mode="json") | (
        {"recovered": True} if not dispatch else {}
    )


async def _native_shell_result(
    runtime: ControlRuntime,
    intent: Intent,
    call: ModelToolCall,
    *,
    dispatch: bool,
) -> tuple[str, dict]:
    session_id = intent.session_id
    if session_id is None:
        raise PermissionError("Missing native Shell Session reservation")
    coordinator = runtime.session_coordinator
    record = coordinator.require_session_status(session_id, {"active"})
    if record.executor_id != intent.machine:
        raise PermissionError("Native Shell Session binding mismatch")
    if dispatch:
        request = reserved_shell_request(call, intent.key)
        payload = await coordinator.call_session_tool(
            SHELL_START_RESERVED_OP,
            request.model_dump(mode="json") | {"session_id": session_id},
        )
        model = (
            ShellExecutionOutput if call.name == "bash" else RunPythonCodeOutput
        )
        result = model.model_validate(payload)
        if result.mode != "pty" or result.result.get("shell_id") != intent.key:
            raise NativeCreationUncertain(
                "Native Shell response has a mismatched identity"
            )
        return intent.key, result.model_dump(mode="json")
    try:
        receipt = ReservedShellLookupResult.model_validate(
            await coordinator.call_session_tool(
                SHELL_LOOKUP_RESERVED_OP,
                {"session_id": session_id, "shell_id": intent.key},
            )
        )
    except PublicToolError, RuntimeError, ValueError, KeyError, TimeoutError:
        raise NativeCreationUncertain(
            "Native Shell creation is unconfirmed"
        ) from None
    if (
        receipt.session_id != session_id
        or receipt.shell_id != intent.key
        or not receipt.present
    ):
        raise NativeCreationUncertain("Native Shell creation is unconfirmed")
    return intent.key, {
        "mode": "pty",
        "result": {"shell_id": intent.key},
        "recovered": True,
        "message": "Existing Shell creation confirmed by native lookup; reuse this shell_id.",
    }


async def _native_copy_result(
    runtime: ControlRuntime,
    intent: Intent,
    call: ModelToolCall,
    *,
    dispatch: bool,
) -> tuple[str, dict]:
    request = ReservedCopyRequest.model_validate(call.arguments)
    if (
        intent.session_id != request.src_session_id
        or intent.destination_session_id != request.dst_session_id
        or intent.destination_machine is None
    ):
        raise PermissionError("Managed copy reservation binding mismatch")
    if dispatch:
        result = await runtime.session_copy_service.start_background(
            **request.model_dump(exclude={"background"}),
            reserved_job_id=intent.key,
            expected_executors={
                request.src_session_id: intent.machine,
                request.dst_session_id: intent.destination_machine,
            },
        )
    else:
        try:
            tail = await managed_job_tail_execute(
                request.src_session_id,
                intent.key,
                expected_kind=SESSION_COPY_MANAGED_KIND,
                expected_payload={
                    "src_session_id": request.src_session_id,
                    "dst_session_id": request.dst_session_id,
                },
            )
        except (
            PublicToolError,
            RuntimeError,
            ValueError,
            KeyError,
            TimeoutError,
        ):
            raise NativeCreationUncertain(
                "Managed copy creation is unconfirmed"
            ) from None
        result = JobStartOutput(**tail.job.model_dump())
    if (
        result.job_id != intent.key
        or result.session_id != intent.session_id
        or result.kind != "managed"
        or result.attempts != 1
    ):
        raise NativeCreationUncertain(
            "Managed copy receipt is mismatched or superseded"
        )
    return intent.key, result.model_dump(mode="json") | (
        {"recovered": True} if not dispatch else {}
    )


async def _native_result(
    runtime: ControlRuntime,
    intent: Intent,
    call: ModelToolCall,
    *,
    dispatch: bool,
) -> tuple[str, dict]:
    arguments = call.arguments
    if call.name in {"bash", "run_python_code"}:
        if arguments.get("pty") is True:
            return await _native_shell_result(
                runtime, intent, call, dispatch=dispatch
            )
        return await _native_job_result(
            runtime, intent, call, dispatch=dispatch
        )
    if call.name == "session_copy":
        return await _native_copy_result(
            runtime, intent, call, dispatch=dispatch
        )
    if call.name == "job":
        return await _native_retry_result(runtime, intent, dispatch=dispatch)
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


async def _dispatch_intent(
    runtime: ControlRuntime,
    repository: DurableTaskRepository,
    lease: ActorLease,
    intent: Intent,
    call: ModelToolCall,
    *,
    scopes: set[str],
    expires: int | None,
) -> dict[str, Any]:
    dispatch = repository.claim(
        lease,
        intent,
        call,
        scopes=scopes,
        expires=expires,
        registered=_registered(runtime),
    )
    # Only batch reservation can return a definitively refused stopped intent.
    if intent.state == "stopped":
        raise NativeRetryRejected("Native Job retry was not admitted")
    try:
        native_id, result = await asyncio.wait_for(
            _native_result(runtime, intent, call, dispatch=dispatch),
            timeout=tool_timeout_s(
                call.name, config=runtime.config, args=call.arguments
            ),
        )
    except NativeRetryRejected:
        repository.reject_retry(lease, intent)
        raise
    confirmed = repository.bind(lease, intent, native_id)
    if confirmed.state != "active":
        raise PermissionError("Native creation was cancelled")
    return result


async def _dispatch_intents(
    runtime: ControlRuntime,
    repository: DurableTaskRepository,
    lease: ActorLease,
    intents: list[Intent],
    call: ModelToolCall,
    *,
    scopes: set[str],
    expires: int | None,
) -> dict[str, Any]:
    # The native Session lock serializes retries. Settle all admitted siblings
    # independently, even when one response is lost; propagate the first error
    # after persisting the remaining receipts, never replay a claimed attempt.
    results = []
    errors = []
    for intent in intents:
        try:
            results.append(
                await _dispatch_intent(
                    runtime,
                    repository,
                    lease,
                    intent,
                    call,
                    scopes=scopes,
                    expires=expires,
                )
            )
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise errors[0]
    if len(results) == 1:
        return results[0]
    retried = [
        job
        for result in results
        for job in JobOutput.model_validate(result).retried
    ]
    return JobOutput(operation="retry", retried=retried).model_dump(
        mode="json"
    ) | (
        {"recovered": True}
        if any(result.get("recovered") is True for result in results)
        else {}
    )


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
    """Create once or recover reserved native work using the authenticated principal."""
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
    destination_machine = None
    if call.name == "session_copy":
        request = ReservedCopyRequest.model_validate(call.arguments)
        source = runtime.session_coordinator.require_session_status(
            request.src_session_id, {"active"}
        )
        destination = runtime.session_coordinator.require_session_status(
            request.dst_session_id, {"active"}
        )
        if str(source.executor_id) != machine:
            raise PermissionError(
                "Managed copy source Executor binding mismatch"
            )
        destination_machine = str(destination.executor_id)
    intents = repository.reserve_all(
        lease,
        call,
        machine=machine,
        scopes=scopes,
        expires=expires,
        registered=_registered(runtime),
        destination_machine=destination_machine,
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
            with audit_call_context(audit_id, sessions, tasks):
                result = await _dispatch_intents(
                    runtime,
                    repository,
                    lease,
                    intents,
                    call,
                    scopes=scopes,
                    expires=expires,
                )
            # The receipt is durable even when access was revoked in flight.
            # Recheck the current authorization before returning it to the model.
            repository.reserve_all(
                lease,
                call,
                machine=machine,
                scopes=scopes,
                expires=expires,
                registered=_registered(runtime),
                destination_machine=destination_machine,
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
