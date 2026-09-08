"""Executor reconnect, heartbeat, polling, and ordinary command execution."""

from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import JsonValue, TypeAdapter

from ..errors import tool_error_payload
from ..protocol.errors import ProtocolErrorCode
from ..protocol.executor import (
    ExecutorCommand,
    ExecutorHelloRequest,
    ExecutorHelloResponse,
    ExecutorResult,
    OperationError,
)
from ..utils.serialization import to_jsonable
from .control_client import ExecutorControlClient, ExecutorControlError
from .errors import ExecutorOperationFailure

_JSON_VALUE = TypeAdapter(JsonValue)
_INITIAL_RETRY_DELAY_S = 0.5
_MAX_RETRY_DELAY_S = 30.0


def executor_retry_delay(attempt: int, random_value: float) -> float:
    """Return bounded exponential reconnect delay with symmetric jitter."""
    base = min(
        _INITIAL_RETRY_DELAY_S * (2 ** min(max(0, attempt), 10)),
        _MAX_RETRY_DELAY_S,
    )
    jittered = base * (0.75 + 0.5 * random_value)
    return min(_MAX_RETRY_DELAY_S, max(0.0, jittered))


type CommandExecutor = Callable[[ExecutorCommand], Any]
type HelloFactory = Callable[[], ExecutorHelloRequest]
type Sleep = Callable[[float], Awaitable[None]]


class _ControlClient(Protocol):
    async def hello(
        self, message: ExecutorHelloRequest
    ) -> ExecutorHelloResponse: ...

    async def heartbeat(self) -> None: ...

    async def poll(self, *, timeout_s: float) -> ExecutorCommand | None: ...

    async def submit_result(self, result: ExecutorResult) -> None: ...

    async def aclose(self) -> None: ...


class ExecutorOwnerActionRequired(RuntimeError):
    """The persisted executor profile cannot reconnect without owner action."""


def operation_error_from_exception(exc: Exception) -> OperationError:
    """Serialize one executor failure without erasing stable public error semantics."""
    message = str(exc).strip() or type(exc).__name__
    if isinstance(exc, ExecutorOperationFailure):
        return OperationError(code=exc.code, message=message[:1000])
    if isinstance(exc, NotImplementedError):
        return OperationError(
            code=ProtocolErrorCode.OPERATION_UNSUPPORTED.value,
            message=message[:1000],
        )
    public_error = {
        str(key): _JSON_VALUE.validate_python(to_jsonable(value))
        for key, value in tool_error_payload(exc).items()
    }
    return OperationError(
        code="operation_failed",
        message=message[:1000],
        data=public_error,
    )


class ExecutorConnection:
    """Run one bounded executor v1 delivery loop using a persisted profile."""

    def __init__(
        self,
        client: _ControlClient,
        *,
        hello_factory: HelloFactory,
        execute: CommandExecutor,
        max_concurrent_commands: int,
        sleep: Sleep = asyncio.sleep,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        if max_concurrent_commands < 1:
            raise ValueError("max_concurrent_commands must be at least one")
        self._client = client
        self._hello_factory = hello_factory
        self._execute = execute
        self._max_concurrent_commands = max_concurrent_commands
        self._sleep = sleep
        self._random_value = random_value
        self._stop = asyncio.Event()
        self._owner_action = asyncio.Event()
        self._owner_action_error: ExecutorOwnerActionRequired | None = None
        self._command_tasks: set[asyncio.Task[None]] = set()
        self._session_mutation_tails: dict[str, asyncio.Future[None]] = {}
        self._main_task: asyncio.Task[None] | None = None

    @classmethod
    def from_client(
        cls,
        client: ExecutorControlClient,
        *,
        hello_factory: HelloFactory,
        execute: CommandExecutor,
        max_concurrent_commands: int,
    ) -> ExecutorConnection:
        """Construct the normal production connection without extra abstractions."""
        return cls(
            client,
            hello_factory=hello_factory,
            execute=execute,
            max_concurrent_commands=max_concurrent_commands,
        )

    @property
    def owner_action_error(self) -> ExecutorOwnerActionRequired | None:
        return self._owner_action_error

    @property
    def active_command_count(self) -> int:
        return len(self._command_tasks)

    def start(self) -> None:
        if self._main_task is not None:
            raise RuntimeError("executor connection is already running")
        self._main_task = asyncio.create_task(
            self.run_forever(), name="workgate-executor-connection"
        )

    async def wait_owner_action(self) -> ExecutorOwnerActionRequired:
        await self._owner_action.wait()
        assert self._owner_action_error is not None
        return self._owner_action_error

    async def run_forever(self) -> None:
        """Reconnect indefinitely until shutdown or a profile/credential failure."""
        attempt = 0
        while not self._stop.is_set() and self._owner_action_error is None:
            # Hello proves auth/protocol compatibility, not that long-poll delivery is
            # healthy enough to reset the reconnect failure streak.
            delivery_progress = asyncio.Event()
            try:
                policy = await self._client.hello(self._hello_factory())
                await self._run_connected(
                    policy, delivery_progress=delivery_progress
                )
            except ExecutorControlError as exc:
                reconnectable = (
                    exc.retryable
                    or exc.code is ProtocolErrorCode.EXECUTOR_OVERLOADED
                )
                if exc.requires_owner_action or not reconnectable:
                    self._require_owner_action(exc)
                    break
                # A duplicate-poll 409 is request-level non-retryable but can be a
                # transient reconnect overlap while control's previous poll unwinds.
                if delivery_progress.is_set():
                    attempt = 0
                await self._sleep(self._retry_delay(attempt))
                attempt += 1

        if self._owner_action_error is not None and not self._stop.is_set():
            # Stay quiescent instead of creating a tight service-manager restart loop.
            await self._stop.wait()

    async def aclose(self) -> None:
        self._stop.set()
        main = self._main_task
        if main is not None:
            main.cancel()
            await asyncio.gather(main, return_exceptions=True)
            self._main_task = None
        tasks = tuple(self._command_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._command_tasks.clear()
        await self._client.aclose()

    async def _run_connected(
        self,
        policy: ExecutorHelloResponse,
        *,
        delivery_progress: asyncio.Event,
    ) -> None:
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(policy), name="workgate-executor-heartbeat"
        )
        poll = asyncio.create_task(
            self._poll_loop(policy, delivery_progress=delivery_progress),
            name="workgate-executor-poll",
        )
        owner_action = asyncio.create_task(self._owner_action.wait())
        stop = asyncio.create_task(self._stop.wait())
        watched: set[asyncio.Task[Any]] = {heartbeat, poll, owner_action, stop}
        try:
            done, _ = await asyncio.wait(
                watched, return_when=asyncio.FIRST_COMPLETED
            )
            if owner_action in done or stop in done:
                return
            for task in done:
                task.result()
        finally:
            for task in watched:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*watched, return_exceptions=True)

    async def _heartbeat_loop(self, policy: ExecutorHelloResponse) -> None:
        attempt = 0
        while not self._stop.is_set() and self._owner_action_error is None:
            await self._sleep(float(policy.heartbeat_interval_s))
            if self._stop.is_set() or self._owner_action_error is not None:
                return
            try:
                await self._client.heartbeat()
                attempt = 0
            except ExecutorControlError as exc:
                if not exc.retryable:
                    self._require_owner_action(exc)
                    return
                await self._sleep(self._retry_delay(attempt))
                attempt += 1

    async def _poll_loop(
        self,
        policy: ExecutorHelloResponse,
        *,
        delivery_progress: asyncio.Event,
    ) -> None:
        while not self._stop.is_set() and self._owner_action_error is None:
            await self._wait_for_capacity()
            if self._stop.is_set() or self._owner_action_error is not None:
                return
            command = await self._client.poll(timeout_s=policy.poll_timeout_s)
            # A completed poll, including a normal 204/no-command response, proves
            # the delivery path is making progress and may reset reconnect backoff.
            delivery_progress.set()
            if command is None:
                continue
            predecessor: asyncio.Future[None] | None = None
            completion: asyncio.Future[None] | None = None
            if command.session_id is not None and command.op in {
                "session.create",
                "session.terminate",
            }:
                session_id = str(command.session_id)
                predecessor = self._session_mutation_tails.get(session_id)
                completion = asyncio.get_running_loop().create_future()
                self._session_mutation_tails[session_id] = completion
            task = asyncio.create_task(
                self._run_command_ordered(
                    command,
                    predecessor=predecessor,
                    completion=completion,
                ),
                name=f"workgate-executor-command-{command.id}",
            )
            self._command_tasks.add(task)
            task.add_done_callback(self._command_tasks.discard)

    async def _wait_for_capacity(self) -> None:
        while len(self._command_tasks) >= self._max_concurrent_commands:
            done, _ = await asyncio.wait(
                self._command_tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                # Command tasks convert ordinary handler failures into results. Only
                # cancellation or an implementation defect can escape here.
                task.result()

    async def _run_command_ordered(
        self,
        command: ExecutorCommand,
        *,
        predecessor: asyncio.Future[None] | None,
        completion: asyncio.Future[None] | None,
    ) -> None:
        """Preserve receipt order for same-session existence mutations."""
        try:
            if predecessor is not None:
                await asyncio.shield(predecessor)
            await self._run_command(command)
        finally:
            if completion is not None and not completion.done():
                completion.set_result(None)
            if command.session_id is not None and completion is not None:
                session_id = str(command.session_id)
                if self._session_mutation_tails.get(session_id) is completion:
                    self._session_mutation_tails.pop(session_id, None)

    async def _run_command(self, command: ExecutorCommand) -> None:
        try:
            value = await self._execute_command(command)
            result = ExecutorResult(
                id=command.id,
                ok=True,
                result=_JSON_VALUE.validate_python(to_jsonable(value)),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = ExecutorResult(
                id=command.id,
                ok=False,
                error=operation_error_from_exception(exc),
            )
        await self._submit_result_until_terminal(result)

    async def _execute_command(self, command: ExecutorCommand) -> Any:
        if inspect.iscoroutinefunction(self._execute):
            return await self._execute(command)
        value = await asyncio.to_thread(self._execute, command)
        if inspect.isawaitable(value):
            return await value
        return value

    async def _submit_result_until_terminal(
        self, result: ExecutorResult
    ) -> None:
        attempt = 0
        while not self._stop.is_set() and self._owner_action_error is None:
            try:
                await self._client.submit_result(result)
                return
            except ExecutorControlError as exc:
                if exc.code is ProtocolErrorCode.UNKNOWN_COMMAND:
                    return
                if not exc.retryable:
                    self._require_owner_action(exc)
                    return
                await self._sleep(self._retry_delay(attempt))
                attempt += 1

    def _require_owner_action(self, exc: ExecutorControlError) -> None:
        if self._owner_action_error is not None:
            return
        code = (
            exc.code.value
            if exc.code is not None
            else "control_rejected_request"
        )
        self._owner_action_error = ExecutorOwnerActionRequired(
            f"executor connection requires owner action: {code}"
        )
        self._owner_action.set()

    def _retry_delay(self, attempt: int) -> float:
        return executor_retry_delay(attempt, self._random_value())
