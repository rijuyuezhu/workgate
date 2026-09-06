import asyncio
import threading

import pytest

from workgate.executor.connection import (
    ExecutorConnection,
    executor_retry_delay,
)
from workgate.executor.control_client import ExecutorControlError
from workgate.protocol.errors import ProtocolError, ProtocolErrorCode
from workgate.protocol.executor import (
    ExecutorCommand,
    ExecutorHelloRequest,
    ExecutorHelloResponse,
    ExecutorResult,
    ExecutorRuntimeSummary,
)
from workgate.protocol.ids import new_command_id


def _hello() -> ExecutorHelloRequest:
    return ExecutorHelloRequest(
        runtime=ExecutorRuntimeSummary(workgate_version="test"),
        sessions=(),
        shells=(),
        jobs=(),
    )


def _policy() -> ExecutorHelloResponse:
    return ExecutorHelloResponse(
        heartbeat_interval_s=1,
        offline_after_s=3,
        poll_timeout_s=1,
    )


def _protocol_error(
    code: ProtocolErrorCode, *, status: int
) -> ExecutorControlError:
    return ExecutorControlError(
        f"control error: {code.value}",
        status_code=status,
        protocol_error=ProtocolError(code=code, message=code.value),
    )


class _BaseFakeClient:
    closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_saturated_blocking_handler_does_not_starve_heartbeat() -> None:
    command = ExecutorCommand(id=new_command_id(), op="test.block")
    release = threading.Event()
    heartbeat_seen = asyncio.Event()
    result_seen = asyncio.Event()

    class FakeClient(_BaseFakeClient):
        hello_calls = 0
        poll_calls = 0
        heartbeat_calls = 0

        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            self.hello_calls += 1
            return _policy()

        async def heartbeat(self) -> None:
            self.heartbeat_calls += 1
            if self.heartbeat_calls >= 2:
                heartbeat_seen.set()

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            self.poll_calls += 1
            if self.poll_calls == 1:
                return command
            await asyncio.Event().wait()
            return None

        async def submit_result(self, result: ExecutorResult) -> None:
            result_seen.set()

    client = FakeClient()

    def blocking_handler(offered: ExecutorCommand) -> str:
        assert offered == command
        assert release.wait(timeout=1.0)
        return "done"

    async def yielding_sleep(delay: float) -> None:
        await asyncio.sleep(0)

    connection = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=blocking_handler,
        max_concurrent_commands=1,
        sleep=yielding_sleep,
        random_value=lambda: 0.5,
    )
    connection.start()
    try:
        await asyncio.wait_for(heartbeat_seen.wait(), timeout=0.5)
        assert client.poll_calls == 1
        assert connection.active_command_count == 1
        release.set()
        await asyncio.wait_for(result_seen.wait(), timeout=0.5)
    finally:
        release.set()
        await connection.aclose()

    assert client.heartbeat_calls >= 2
    assert client.closed


@pytest.mark.asyncio
async def test_transient_poll_failure_reconnects_with_fresh_hello_without_replay() -> (
    None
):
    reconnected = asyncio.Event()
    repoll_started = asyncio.Event()

    class FakeClient(_BaseFakeClient):
        hello_calls = 0
        poll_calls = 0

        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            self.hello_calls += 1
            if self.hello_calls >= 2:
                reconnected.set()
            return _policy()

        async def heartbeat(self) -> None:
            return None

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            self.poll_calls += 1
            if self.poll_calls == 1:
                raise ExecutorControlError("temporary network failure")
            repoll_started.set()
            await asyncio.Event().wait()
            return None

        async def submit_result(self, result: ExecutorResult) -> None:
            raise AssertionError("no result should be submitted")

    async def yielding_sleep(delay: float) -> None:
        await asyncio.sleep(0)

    client = FakeClient()
    connection = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=lambda command: None,
        max_concurrent_commands=1,
        sleep=yielding_sleep,
        random_value=lambda: 0.5,
    )
    connection.start()
    try:
        await asyncio.wait_for(reconnected.wait(), timeout=0.5)
        await asyncio.wait_for(repoll_started.wait(), timeout=0.5)
        assert client.hello_calls == 2
        assert client.poll_calls == 2
        assert connection.active_command_count == 0
    finally:
        await connection.aclose()


@pytest.mark.asyncio
async def test_revoked_credential_quiesces_instead_of_reconnect_loop() -> None:
    class FakeClient(_BaseFakeClient):
        hello_calls = 0

        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            self.hello_calls += 1
            raise _protocol_error(
                ProtocolErrorCode.EXECUTOR_REVOKED, status=403
            )

        async def heartbeat(self) -> None:
            raise AssertionError("heartbeat must not start")

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            raise AssertionError("poll must not start")

        async def submit_result(self, result: ExecutorResult) -> None:
            raise AssertionError("result must not submit")

    client = FakeClient()
    connection = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=lambda command: None,
        max_concurrent_commands=1,
    )
    connection.start()
    error = await asyncio.wait_for(connection.wait_owner_action(), timeout=0.5)
    await asyncio.sleep(0.01)

    assert "executor_revoked" in str(error)
    assert client.hello_calls == 1
    await connection.aclose()


@pytest.mark.asyncio
async def test_transient_result_upload_retries_without_reexecuting_command() -> (
    None
):
    command = ExecutorCommand(id=new_command_id(), op="test.echo")
    result_accepted = asyncio.Event()
    executed = 0

    class FakeClient(_BaseFakeClient):
        poll_calls = 0
        results: list[ExecutorResult] = []

        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            return _policy()

        async def heartbeat(self) -> None:
            return None

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            self.poll_calls += 1
            if self.poll_calls == 1:
                return command
            await asyncio.Event().wait()
            return None

        async def submit_result(self, result: ExecutorResult) -> None:
            self.results.append(result)
            if len(self.results) == 1:
                raise ExecutorControlError("temporary result upload failure")
            result_accepted.set()

    def execute(offered: ExecutorCommand) -> dict[str, str]:
        nonlocal executed
        executed += 1
        return {"id": offered.id}

    async def yielding_sleep(delay: float) -> None:
        await asyncio.sleep(0)

    client = FakeClient()
    connection = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=execute,
        max_concurrent_commands=1,
        sleep=yielding_sleep,
        random_value=lambda: 0.5,
    )
    connection.start()
    try:
        await asyncio.wait_for(result_accepted.wait(), timeout=0.5)
        assert executed == 1
        assert len(client.results) == 2
        assert client.results[0] == client.results[1]
    finally:
        await connection.aclose()


@pytest.mark.asyncio
async def test_unknown_command_is_terminal_for_result_upload() -> None:
    command = ExecutorCommand(id=new_command_id(), op="test.echo")
    result_attempted = asyncio.Event()

    class FakeClient(_BaseFakeClient):
        poll_calls = 0
        result_calls = 0

        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            return _policy()

        async def heartbeat(self) -> None:
            return None

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            self.poll_calls += 1
            if self.poll_calls == 1:
                return command
            await asyncio.Event().wait()
            return None

        async def submit_result(self, result: ExecutorResult) -> None:
            self.result_calls += 1
            result_attempted.set()
            raise _protocol_error(ProtocolErrorCode.UNKNOWN_COMMAND, status=404)

    async def yielding_sleep(delay: float) -> None:
        await asyncio.sleep(0)

    client = FakeClient()
    connection = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=lambda offered: {"id": offered.id},
        max_concurrent_commands=1,
        sleep=yielding_sleep,
    )
    connection.start()
    try:
        await asyncio.wait_for(result_attempted.wait(), timeout=0.5)
        for _ in range(5):
            await asyncio.sleep(0)
        assert client.result_calls == 1
        assert connection.owner_action_error is None
    finally:
        await connection.aclose()


@pytest.mark.asyncio
async def test_close_interrupts_long_reconnect_backoff() -> None:
    retry_sleep_started = asyncio.Event()

    class FakeClient(_BaseFakeClient):
        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            raise ExecutorControlError("temporary control outage")

        async def heartbeat(self) -> None:
            raise AssertionError("heartbeat must not start")

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            raise AssertionError("poll must not start")

        async def submit_result(self, result: ExecutorResult) -> None:
            raise AssertionError("result must not submit")

    async def blocked_sleep(delay: float) -> None:
        retry_sleep_started.set()
        await asyncio.Event().wait()

    client = FakeClient()
    connection = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=lambda command: None,
        max_concurrent_commands=1,
        sleep=blocked_sleep,
    )
    connection.start()
    await asyncio.wait_for(retry_sleep_started.wait(), timeout=0.5)

    await asyncio.wait_for(connection.aclose(), timeout=0.5)

    assert client.closed


def test_reconnect_backoff_is_exponential_jittered_and_bounded() -> None:
    assert executor_retry_delay(0, 0.5) == 0.5
    assert executor_retry_delay(1, 0.5) == 1.0
    assert executor_retry_delay(20, 0.5) == 30.0
