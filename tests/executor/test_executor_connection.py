import asyncio
import logging
import threading
from typing import cast

import httpx
import pytest

from workgate import __version__
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor import control_client as control_client_module
from workgate.executor.connection import (
    ExecutorConnection,
    RuntimePolicyAction,
    executor_retry_delay,
)
from workgate.executor.control_client import (
    ExecutorControlClient,
    ExecutorControlError,
)
from workgate.executor.errors import ExecutorResourceInventoryUnavailable
from workgate.executor.profile import ExecutorProfile
from workgate.executor.runtime import build_executor_runtime
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.errors import ProtocolError, ProtocolErrorCode
from workgate.protocol.executor import (
    EXECUTOR_CAPABILITY_GUI,
    EXECUTOR_RESULT_PATH,
    ExecutorCommand,
    ExecutorHeartbeatResponse,
    ExecutorHelloRequest,
    ExecutorHelloResponse,
    ExecutorResult,
    ExecutorRuntimeSummary,
)
from workgate.protocol.ids import new_command_id, new_executor_id


def _hello() -> ExecutorHelloRequest:
    return ExecutorHelloRequest(
        runtime=ExecutorRuntimeSummary(workgate_version="test"),
        default_workdir="/workspace",
        sessions=(),
        shells=(),
        jobs=(),
    )


def _policy() -> ExecutorHelloResponse:
    return ExecutorHelloResponse(
        heartbeat_interval_s=1,
        offline_after_s=3,
        poll_timeout_s=1,
        required_workgate_version=__version__,
        runtime_update_required=False,
    )


def _reconnect_policy() -> ExecutorHelloResponse:
    return ExecutorHelloResponse(
        heartbeat_interval_s=100,
        offline_after_s=300,
        poll_timeout_s=1,
        required_workgate_version=__version__,
        runtime_update_required=False,
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


class _ScriptedReconnectClient(_BaseFakeClient):
    def __init__(self, poll_outcomes: tuple[Exception | None, ...]) -> None:
        self._poll_outcomes = poll_outcomes
        self.recovered = asyncio.Event()
        self.hello_calls = 0
        self.poll_calls = 0

    async def hello(
        self, message: ExecutorHelloRequest
    ) -> ExecutorHelloResponse:
        self.hello_calls += 1
        return _reconnect_policy()

    async def heartbeat(self) -> None:
        return None

    async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
        self.poll_calls += 1
        index = self.poll_calls - 1
        if index < len(self._poll_outcomes):
            outcome = self._poll_outcomes[index]
            if outcome is not None:
                raise outcome
            return None
        self.recovered.set()
        await asyncio.Event().wait()
        return None

    async def submit_result(self, result: ExecutorResult) -> None:
        raise AssertionError("no result should be submitted")


async def _exercise_reconnect(
    poll_outcomes: tuple[Exception | None, ...],
) -> tuple[_ScriptedReconnectClient, list[float]]:
    retry_sleeps: list[float] = []

    async def controlled_sleep(delay: float) -> None:
        if delay == 100.0:
            await asyncio.Event().wait()
        retry_sleeps.append(delay)
        await asyncio.sleep(0)

    client = _ScriptedReconnectClient(poll_outcomes)
    connection = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=lambda command: None,
        max_concurrent_commands=1,
        sleep=controlled_sleep,
        random_value=lambda: 0.5,
    )
    connection.start()
    try:
        await asyncio.wait_for(client.recovered.wait(), timeout=0.5)
        assert connection.owner_action_error is None
        return client, retry_sleeps
    finally:
        await connection.aclose()


@pytest.mark.asyncio
async def test_failed_control_request_reloads_proxy_settings_without_restarting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A replacement AsyncClient must use the proxy route selected at creation.
    real_client = httpx.AsyncClient
    routes: list[str] = []
    created: list[httpx.AsyncClient] = []
    current_route = ["old"]

    def factory(**kwargs: object) -> httpx.AsyncClient:
        route = current_route[0]
        routes.append(route)

        def respond(request: httpx.Request) -> httpx.Response:
            if route == "old":
                raise httpx.ConnectError(
                    "private request URL or bearer", request=request
                )
            return httpx.Response(204)

        instance = real_client(
            base_url=cast(str, kwargs["base_url"]),
            headers=cast(dict[str, str], kwargs["headers"]),
            transport=httpx.MockTransport(respond),
        )
        created.append(instance)
        return instance

    monkeypatch.setattr(control_client_module.httpx, "AsyncClient", factory)
    profile = ExecutorProfile(
        control_url="https://control.example",
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )
    client = ExecutorControlClient(profile)
    try:
        with pytest.raises(ExecutorControlError) as caught:
            await client.heartbeat()
        assert caught.value.transport_error == "ConnectError"
        current_route[0] = "new"
        await client.heartbeat()
        assert routes == ["old", "new"]
        assert created[0].is_closed
        assert not created[1].is_closed
    finally:
        await client.aclose()
    assert created[1].is_closed


@pytest.mark.asyncio
async def test_transport_refresh_keeps_inflight_result_upload_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    finish = asyncio.Event()
    created: list[httpx.AsyncClient] = []
    real_client = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        index = len(created)

        async def respond(request: httpx.Request) -> httpx.Response:
            if index == 0 and request.url.path == EXECUTOR_RESULT_PATH:
                started.set()
                await finish.wait()
                return httpx.Response(204)
            if index == 0:
                raise httpx.ReadTimeout("a private URL", request=request)
            return httpx.Response(204)

        instance = real_client(
            base_url=cast(str, kwargs["base_url"]),
            headers=cast(dict[str, str], kwargs["headers"]),
            transport=httpx.MockTransport(respond),
        )
        created.append(instance)
        return instance

    monkeypatch.setattr(control_client_module.httpx, "AsyncClient", factory)
    profile = ExecutorProfile(
        control_url="https://control.example",
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )
    client = ExecutorControlClient(profile)
    result = ExecutorResult(id=new_command_id(), ok=True, result={"ok": True})
    upload = asyncio.create_task(client.submit_result(result))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        with pytest.raises(ExecutorControlError) as caught:
            await client.validate()
        assert caught.value.transport_error == "ReadTimeout"
        await client.validate()
        assert len(created) == 2
        assert not created[0].is_closed
        finish.set()
        await asyncio.wait_for(upload, timeout=1)
        assert created[0].is_closed
        assert not created[1].is_closed
    finally:
        finish.set()
        await asyncio.gather(upload, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_result_http_413_returns_small_failure_without_owner_action() -> (
    None
):
    """A rejected result must resolve the offered command and not strand the fleet."""
    received: list[ExecutorResult] = []
    requests: list[int] = []

    def proxy(request: httpx.Request) -> httpx.Response:
        assert request.url.path == EXECUTOR_RESULT_PATH
        requests.append(len(request.content))
        if len(request.content) > 1024 * 1024:
            return httpx.Response(413)
        received.append(ExecutorResult.model_validate_json(request.content))
        return httpx.Response(204)

    profile = ExecutorProfile(
        control_url="https://control.example",
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )
    http = httpx.AsyncClient(
        base_url=profile.control_url, transport=httpx.MockTransport(proxy)
    )
    client = ExecutorControlClient(profile, client=http)
    connection = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=lambda command: None,
        max_concurrent_commands=1,
    )
    command_id = new_command_id()
    try:
        assert await connection._submit_result_until_terminal(
            ExecutorResult(
                id=command_id, ok=True, result={"data_b64": "A" * (1024 * 1024)}
            )
        )
        assert requests[0] > 1024 * 1024
        assert requests[1] < 1024 * 1024
        assert len(requests) == 2
        assert len(received) == 1
        assert received[0].id == command_id
        assert not received[0].ok
        assert received[0].error is not None
        assert received[0].error.code == "result_upload_rejected"
        assert "HTTP 413" in received[0].error.message
        assert "client_max_body_size" in received[0].error.message
        assert connection.owner_action_error is None
        # Ordinary commands still upload normally after the rejected result.
        assert await connection._submit_result_until_terminal(
            ExecutorResult(id=new_command_id(), ok=True, result={"ok": True})
        )
        assert received[-1].ok
        assert connection.owner_action_error is None
    finally:
        await client.aclose()
        await http.aclose()


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
async def test_same_transfer_abandonment_waits_for_earlier_offered_begin(
    tmp_path,
) -> None:
    transfer_id = "copy_" + "a" * 22
    session_id = "sess_0000000000000000000001"
    workspace = tmp_path / "workspace"
    workdir = workspace / "session"
    workdir.mkdir(parents=True)
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                default_workdir=workspace,
                state_dir=tmp_path / "state",
                agent_bridge_enabled=False,
            )
        ),
        enable_control_connection=False,
    )
    runtime.services.tool_session_store.create_session(
        session_id=session_id, workdir=workdir
    )
    begin = ExecutorCommand(
        id=new_command_id(),
        op="transfer_begin_write",
        session_id=session_id,
        args={"path": "result.bin", "transfer_id": transfer_id},
    )
    abandon = ExecutorCommand(
        id=new_command_id(),
        op="transfer_abandon_import",
        args={
            "transfer_id": transfer_id,
            "kind": "file",
            "import_path": "result.bin",
        },
    )
    second_offered = asyncio.Event()
    begin_started = asyncio.Event()
    abandon_started = asyncio.Event()
    release_begin = asyncio.Event()
    results_seen = asyncio.Event()
    execution_order: list[str] = []

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
                return begin
            if self.poll_calls == 2:
                second_offered.set()
                return abandon
            await asyncio.Event().wait()
            return None

        async def submit_result(self, result: ExecutorResult) -> None:
            self.results.append(result)
            if len(self.results) == 2:
                results_seen.set()

    async def execute(command: ExecutorCommand):
        if command.id == begin.id:
            execution_order.append("begin")
            begin_started.set()
            await release_begin.wait()
        else:
            assert command.id == abandon.id
            execution_order.append("abandon")
            abandon_started.set()
        return await runtime._execute_protocol_command(command)

    client = FakeClient()
    connection = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=execute,
        max_concurrent_commands=2,
    )
    connection.start()
    try:
        await asyncio.wait_for(begin_started.wait(), timeout=0.5)
        await asyncio.wait_for(second_offered.wait(), timeout=0.5)
        for _ in range(3):
            await asyncio.sleep(0)
        assert not abandon_started.is_set()

        release_begin.set()
        await asyncio.wait_for(abandon_started.wait(), timeout=0.5)
        await asyncio.wait_for(results_seen.wait(), timeout=0.5)
        assert execution_order == ["begin", "abandon"]
        abandon_result = next(
            row for row in client.results if row.id == abandon.id
        )
        assert abandon_result.ok is True
        assert isinstance(abandon_result.result, dict)
        assert abandon_result.result["safe_to_forget"] is True
        receipt_path = (
            runtime.services.state_store.layout.executor_transfers_dir
            / f"write-{transfer_id}.json"
        )
        temporary = workdir / f".result.bin.workgate-transfer-{transfer_id}.tmp"
        assert not receipt_path.exists()
        assert not temporary.exists()
        assert not temporary.with_name(temporary.name + ".json").exists()
    finally:
        release_begin.set()
        await connection.aclose()


@pytest.mark.asyncio
async def test_non_authoritative_resource_inventory_retries_before_hello() -> (
    None
):
    hello_seen = asyncio.Event()
    factory_calls = 0
    retry_sleeps: list[float] = []

    class FakeClient(_BaseFakeClient):
        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            assert message == _hello()
            hello_seen.set()
            return _policy()

        async def heartbeat(self) -> None:
            return None

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            await asyncio.Event().wait()
            return None

        async def submit_result(self, result: ExecutorResult) -> None:
            raise AssertionError("no result should be submitted")

    async def hello_factory() -> ExecutorHelloRequest:
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            raise ExecutorResourceInventoryUnavailable("inventory uncertain")
        return _hello()

    async def yielding_sleep(delay: float) -> None:
        retry_sleeps.append(delay)
        await asyncio.sleep(0)

    connection = ExecutorConnection(
        FakeClient(),
        hello_factory=hello_factory,
        execute=lambda command: None,
        max_concurrent_commands=1,
        sleep=yielding_sleep,
        random_value=lambda: 0.5,
    )
    connection.start()
    try:
        await asyncio.wait_for(hello_seen.wait(), timeout=0.5)
        assert factory_calls == 2
        assert retry_sleeps == [0.5]
        assert connection.owner_action_error is None
    finally:
        await connection.aclose()


@pytest.mark.asyncio
async def test_transient_local_hello_timeout_retries_before_connecting() -> (
    None
):
    connected = asyncio.Event()
    retry_sleeps: list[float] = []
    factory_calls = 0

    class Client(_BaseFakeClient):
        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            connected.set()
            return _policy()

        async def heartbeat(self) -> None:
            return None

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            await asyncio.Event().wait()
            return None

        async def submit_result(self, result: ExecutorResult) -> None:
            raise AssertionError("no result expected")

    def hello_factory() -> ExecutorHelloRequest:
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            raise TimeoutError("temporary inventory I/O timeout")
        return _hello()

    async def yielding_sleep(delay: float) -> None:
        retry_sleeps.append(delay)
        await asyncio.sleep(0)

    connection = ExecutorConnection(
        Client(),
        hello_factory=hello_factory,
        execute=lambda command: None,
        max_concurrent_commands=1,
        sleep=yielding_sleep,
        random_value=lambda: 0.5,
    )
    connection.start()
    try:
        await asyncio.wait_for(connected.wait(), timeout=0.5)
        assert factory_calls == 2
        assert retry_sleeps == [0.5]
        assert connection.owner_action_error is None
    finally:
        await connection.aclose()


@pytest.mark.asyncio
async def test_transient_local_poll_disconnect_retries_with_bounded_backoff() -> (
    None
):
    client, retry_sleeps = await _exercise_reconnect(
        (ConnectionResetError("temporary local socket loss"), TimeoutError())
    )
    assert client.hello_calls == 3
    assert client.poll_calls == 3
    assert retry_sleeps == [0.5, 1.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, PermissionError])
async def test_unexpected_local_reconnect_failure_requires_owner_action(
    error_type: type[Exception],
) -> None:
    class FakeClient(_BaseFakeClient):
        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            raise AssertionError(
                "hello must not run after local inventory failure"
            )

        async def heartbeat(self) -> None:
            raise AssertionError("heartbeat must not start")

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            raise AssertionError("poll must not start")

        async def submit_result(self, result: ExecutorResult) -> None:
            raise AssertionError("result must not submit")

    async def hello_factory() -> ExecutorHelloRequest:
        raise error_type("corrupt durable job inventory")

    connection = ExecutorConnection(
        FakeClient(),
        hello_factory=hello_factory,
        execute=lambda command: None,
        max_concurrent_commands=1,
    )
    connection.start()
    try:
        error = await asyncio.wait_for(
            connection.wait_owner_action(), timeout=0.5
        )
        assert "executor connection failed locally" in str(error)
        assert f"{error_type.__name__}: corrupt durable job inventory" in str(
            error
        )
    finally:
        await connection.aclose()


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


@pytest.mark.parametrize("operation", ("job", "session.create"))
@pytest.mark.asyncio
async def test_reconnect_waits_for_offered_inventory_mutation_execution(
    operation: str,
) -> None:
    command_started = asyncio.Event()
    release_command = asyncio.Event()
    reconnected = asyncio.Event()

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
                return ExecutorCommand(
                    id=new_command_id(),
                    op=operation,
                    session_id="sess_0000000000000000000001",
                    args={"list_jobs": True}
                    if operation == "job"
                    else {"workdir": None},
                )
            if self.poll_calls == 2:
                await command_started.wait()
                raise ExecutorControlError("temporary network failure")
            await asyncio.Event().wait()
            return None

        async def submit_result(self, result: ExecutorResult) -> None:
            return None

    async def execute(command: ExecutorCommand):
        command_started.set()
        await release_command.wait()
        return {"ok": True}

    async def yielding_sleep(delay: float) -> None:
        await asyncio.sleep(0)

    client = FakeClient()
    connection = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=execute,
        max_concurrent_commands=2,
        sleep=yielding_sleep,
        random_value=lambda: 0.5,
    )
    connection.start()
    try:
        await asyncio.wait_for(command_started.wait(), timeout=0.5)
        for _ in range(5):
            await asyncio.sleep(0)
        assert client.hello_calls == 1
        assert not reconnected.is_set()

        release_command.set()
        await asyncio.wait_for(reconnected.wait(), timeout=0.5)
        assert client.hello_calls == 2
    finally:
        await connection.aclose()


@pytest.mark.asyncio
async def test_reconnect_does_not_wait_for_old_inventory_result_upload() -> (
    None
):
    execution_finished = asyncio.Event()
    release_upload = asyncio.Event()
    reconnected = asyncio.Event()

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
                return ExecutorCommand(
                    id=new_command_id(),
                    op="job",
                    session_id="sess_0000000000000000000001",
                    args={"list_jobs": True},
                )
            if self.poll_calls == 2:
                await execution_finished.wait()
                raise ExecutorControlError("temporary network failure")
            await asyncio.Event().wait()
            return None

        async def submit_result(self, result: ExecutorResult) -> None:
            await release_upload.wait()

    async def execute(command: ExecutorCommand):
        execution_finished.set()
        return {"ok": True}

    async def yielding_sleep(delay: float) -> None:
        await asyncio.sleep(0)

    client = FakeClient()
    connection = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=execute,
        max_concurrent_commands=2,
        sleep=yielding_sleep,
        random_value=lambda: 0.5,
    )
    connection.start()
    try:
        await asyncio.wait_for(execution_finished.wait(), timeout=0.5)
        await asyncio.wait_for(reconnected.wait(), timeout=0.5)
        assert client.hello_calls == 2
        assert connection.active_command_count == 1
    finally:
        release_upload.set()
        await connection.aclose()


@pytest.mark.asyncio
async def test_temporary_duplicate_poll_overload_retries_without_owner_action() -> (
    None
):
    client, retry_sleeps = await _exercise_reconnect(
        (
            ExecutorControlError("ambiguous old poll network failure"),
            _protocol_error(ProtocolErrorCode.EXECUTOR_OVERLOADED, status=409),
        )
    )

    assert client.hello_calls == 3
    assert client.poll_calls == 3
    assert retry_sleeps == [0.5, 1.0]


@pytest.mark.asyncio
async def test_poll_incompatible_after_control_restart_rehellos() -> None:
    client, retry_sleeps = await _exercise_reconnect(
        (
            _protocol_error(
                ProtocolErrorCode.EXECUTOR_INCOMPATIBLE, status=400
            ),
            _protocol_error(
                ProtocolErrorCode.EXECUTOR_INCOMPATIBLE, status=400
            ),
        )
    )
    assert client.hello_calls == 3
    assert client.poll_calls == 3
    assert retry_sleeps == [0.5, 1.0]


@pytest.mark.asyncio
async def test_repeated_poll_failures_keep_backoff_across_successful_hello() -> (
    None
):
    client, retry_sleeps = await _exercise_reconnect(
        tuple(
            ExecutorControlError("temporary poll network failure")
            for _ in range(3)
        )
    )

    assert client.hello_calls == 4
    assert client.poll_calls == 4
    assert retry_sleeps == [0.5, 1.0, 2.0]


@pytest.mark.asyncio
async def test_successful_poll_resets_reconnect_backoff_streak() -> None:
    transient = ExecutorControlError("temporary poll network failure")
    client, retry_sleeps = await _exercise_reconnect(
        (transient, transient, None, transient)
    )

    assert client.hello_calls == 4
    assert client.poll_calls == 5
    assert retry_sleeps == [0.5, 1.0, 0.5]


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


@pytest.mark.asyncio
async def test_windows_gui_capability_changes_rehello_on_heartbeat(
    monkeypatch,
) -> None:
    import workgate.executor.connection as connection_module

    available = False
    monkeypatch.setattr(
        connection_module, "gui_capability_available", lambda: available
    )
    hello_messages: asyncio.Queue[ExecutorHelloRequest] = asyncio.Queue()
    heartbeats: asyncio.Queue[None] = asyncio.Queue()
    tick = asyncio.Event()

    class Client(_BaseFakeClient):
        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            await hello_messages.put(message)
            return _policy()

        async def heartbeat(self) -> None:
            await heartbeats.put(None)
            return None

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            await asyncio.Event().wait()
            return None

        async def submit_result(self, result: ExecutorResult) -> None:
            raise AssertionError("no result expected")

    def hello() -> ExecutorHelloRequest:
        return _hello().model_copy(
            update={
                "runtime": ExecutorRuntimeSummary(
                    workgate_version="test", platform="Windows"
                ),
                "capabilities": (EXECUTOR_CAPABILITY_GUI,) if available else (),
            }
        )

    async def sleep(_delay: float) -> None:
        await tick.wait()
        tick.clear()

    connection = ExecutorConnection(
        Client(),
        hello_factory=hello,
        execute=lambda command: None,
        max_concurrent_commands=1,
        sleep=sleep,
    )
    connection.start()
    try:
        first = await asyncio.wait_for(hello_messages.get(), timeout=1)
        assert EXECUTOR_CAPABILITY_GUI not in first.capabilities

        available = True
        tick.set()
        second = await asyncio.wait_for(hello_messages.get(), timeout=1)
        assert EXECUTOR_CAPABILITY_GUI in second.capabilities

        # Stable desktop availability should not trigger another Hello.
        await asyncio.wait_for(heartbeats.get(), timeout=1)
        tick.set()
        await asyncio.wait_for(heartbeats.get(), timeout=1)
        await asyncio.sleep(0)
        assert hello_messages.empty()

        available = False
        tick.set()
        third = await asyncio.wait_for(hello_messages.get(), timeout=1)
        assert EXECUTOR_CAPABILITY_GUI not in third.capabilities
    finally:
        await connection.aclose()


@pytest.mark.asyncio
async def test_heartbeat_hint_rehellos_then_drains_before_update() -> None:
    hinted = asyncio.Event()
    second_hello = asyncio.Event()
    release = asyncio.Event()
    second_policy = asyncio.Event()

    class Client(_BaseFakeClient):
        hello_calls = 0

        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            self.hello_calls += 1
            if self.hello_calls == 1:
                return _policy()
            second_hello.set()
            return ExecutorHelloResponse(
                heartbeat_interval_s=1,
                offline_after_s=3,
                poll_timeout_s=1,
                required_workgate_version="new-release",
                runtime_update_required=True,
            )

        async def heartbeat(self) -> ExecutorHeartbeatResponse:
            hinted.set()
            return ExecutorHeartbeatResponse(
                required_workgate_version="new-release"
            )

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            await asyncio.Event().wait()
            return None

        async def submit_result(self, result: ExecutorResult) -> None:
            raise AssertionError("no result expected")

    async def runtime_policy(
        policy: ExecutorHelloResponse,
    ) -> RuntimePolicyAction:
        if policy.runtime_update_required:
            second_policy.set()
            return RuntimePolicyAction.RESTART
        return RuntimePolicyAction.CONTINUE

    client = Client()
    connection = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=lambda command: None,
        max_concurrent_commands=1,
        runtime_policy_handler=runtime_policy,
        sleep=lambda delay: asyncio.sleep(0),
    )

    async def active_command() -> None:
        await release.wait()

    task = asyncio.create_task(active_command())
    connection._command_tasks.add(task)
    task.add_done_callback(connection._command_tasks.discard)
    connection.start()
    try:
        await asyncio.wait_for(hinted.wait(), timeout=1)
        # The second hello occurs, but its update policy may not interrupt
        # the command already offered under the previous version.
        await asyncio.wait_for(second_hello.wait(), timeout=1)
        assert client.hello_calls == 2
        assert not second_policy.is_set()
        assert not connection.restart_requested

        release.set()
        await asyncio.wait_for(second_policy.wait(), timeout=1)
        await asyncio.wait_for(connection.wait_owner_action(), timeout=1)
        assert connection.restart_requested
    finally:
        release.set()
        await connection.aclose()


@pytest.mark.asyncio
async def test_runtime_update_waits_for_active_commands_then_requests_restart() -> (
    None
):
    release = asyncio.Event()
    handler_called = asyncio.Event()

    class Client(_BaseFakeClient):
        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            return ExecutorHelloResponse(
                heartbeat_interval_s=10,
                offline_after_s=30,
                poll_timeout_s=1,
                required_workgate_version="next-version",
                runtime_update_required=True,
            )

        async def heartbeat(self) -> None:
            raise AssertionError(
                "heartbeat must not start before runtime policy"
            )

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            raise AssertionError("poll must not start before runtime policy")

        async def submit_result(self, result: ExecutorResult) -> None:
            raise AssertionError("no result expected")

    async def active_command() -> None:
        await release.wait()

    async def runtime_policy(
        _policy: ExecutorHelloResponse,
    ) -> RuntimePolicyAction:
        handler_called.set()
        return RuntimePolicyAction.RESTART

    connection = ExecutorConnection(
        Client(),
        hello_factory=_hello,
        execute=lambda _command: None,
        max_concurrent_commands=2,
        runtime_policy_handler=runtime_policy,
    )
    active = asyncio.create_task(active_command())
    connection._command_tasks.add(active)
    active.add_done_callback(connection._command_tasks.discard)
    connection.start()
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not handler_called.is_set()

        release.set()
        owner_action = await asyncio.wait_for(
            connection.wait_owner_action(), timeout=0.5
        )

        assert handler_called.is_set()
        assert connection.restart_requested is True
        assert "restart required" in str(owner_action)
    finally:
        release.set()
        await connection.aclose()


@pytest.mark.asyncio
async def test_blocked_runtime_policy_quiesces_without_requesting_restart() -> (
    None
):
    policy_called = asyncio.Event()

    class Client(_BaseFakeClient):
        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            return ExecutorHelloResponse(
                heartbeat_interval_s=10,
                offline_after_s=30,
                poll_timeout_s=1,
                required_workgate_version="next-version",
                runtime_update_required=True,
            )

        async def heartbeat(self) -> None:
            raise AssertionError("blocked executor must not heartbeat")

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            raise AssertionError("blocked executor must not poll")

        async def submit_result(self, result: ExecutorResult) -> None:
            raise AssertionError("no result expected")

    async def runtime_policy(
        _policy: ExecutorHelloResponse,
    ) -> RuntimePolicyAction:
        policy_called.set()
        return RuntimePolicyAction.BLOCK

    connection = ExecutorConnection(
        Client(),
        hello_factory=_hello,
        execute=lambda _command: None,
        max_concurrent_commands=1,
        runtime_policy_handler=runtime_policy,
    )
    connection.start()
    try:
        await asyncio.wait_for(policy_called.wait(), timeout=0.5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(connection.wait_owner_action(), timeout=0.05)
        assert connection.restart_requested is False
    finally:
        await connection.aclose()


def test_connection_logging_deduplicates_heartbeats_and_redacts_raw_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    observed: list[tuple[str, str | None]] = []
    connection = ExecutorConnection(
        _ScriptedReconnectClient(()),
        hello_factory=_hello,
        execute=lambda _command: None,
        max_concurrent_commands=1,
        status_callback=lambda phase, reason: observed.append((phase, reason)),
    )

    with caplog.at_level(logging.INFO, logger="workgate.executor.connection"):
        connection._report_status("reconnecting")
        connection._report_status("reconnecting", "TimeoutError")
        connection._report_status("reconnecting", "ConnectionResetError")
        connection._report_status("reconnecting", "TimeoutError")
        connection._report_status("connected")
        connection._report_status("connected")
        connection._report_status("reconnecting", "ConnectionError")
        connection._report_status("reconnecting", "TimeoutError")
        connection._report_status("reconnecting", "ConnectionError")
        connection._report_status("connected")
        connection._report_status("action-required", "executor_revoked")
        connection._report_status("action-required", "executor_revoked")

    assert [
        (record.levelname, record.getMessage()) for record in caplog.records
    ] == [
        ("INFO", "executor connecting"),
        ("WARNING", "executor reconnecting: TimeoutError"),
        ("INFO", "executor connection established"),
        ("WARNING", "executor reconnecting: ConnectionError"),
        ("INFO", "executor connection recovered"),
        ("ERROR", "executor requires owner action: executor_revoked"),
    ]
    # Durable status callbacks still run on every heartbeat.
    assert len(observed) == 12

    with caplog.at_level(logging.INFO, logger="workgate.executor.connection"):
        connection._report_status(
            "reconnecting", "secret\nAuthorization: bearer hidden"
        )
    assert caplog.records[-1].getMessage() == "executor reconnecting: unknown"
    assert "Authorization" not in caplog.text
    assert "bearer hidden" not in caplog.text


@pytest.mark.asyncio
async def test_reconnect_warning_identifies_safe_request_status_and_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failure = ExecutorControlError(
        "private response body / Authorization: secret",
        request_kind="poll",
        status_code=429,
        protocol_error=ProtocolError(
            code=ProtocolErrorCode.EXECUTOR_OVERLOADED,
            message="private protocol body",
        ),
    )
    client = _ScriptedReconnectClient((failure, None))
    connection = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=lambda _command: None,
        max_concurrent_commands=1,
        sleep=lambda _delay: asyncio.sleep(0),
    )
    with caplog.at_level(logging.INFO, logger="workgate.executor.connection"):
        connection.start()
        try:
            await asyncio.wait_for(client.recovered.wait(), timeout=2)
        finally:
            await connection.aclose()
    lines = [r.getMessage() for r in caplog.records]
    assert (
        "executor reconnecting: ExecutorControlError "
        "request=poll http=429 code=executor_overloaded transport=none"
    ) in lines
    assert "private" not in caplog.text
    assert "Authorization" not in caplog.text
    assert "secret" not in caplog.text
    assert sum("request=poll" in line for line in lines) == 1


def test_reconnect_warning_includes_only_allowlisted_transport_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection = ExecutorConnection(
        _ScriptedReconnectClient(()),
        hello_factory=_hello,
        execute=lambda _command: None,
        max_concurrent_commands=1,
    )
    with caplog.at_level(
        logging.WARNING, logger="workgate.executor.connection"
    ):
        connection._report_status(
            "reconnecting",
            "ExecutorControlError",
            control_error=ExecutorControlError(
                "private URL: Authorization header must never appear",
                request_kind="heartbeat",
                transport_error="ConnectTimeout",
            ),
        )
    assert (
        caplog.records[-1]
        .getMessage()
        .endswith(
            "request=heartbeat http=unknown code=none transport=ConnectTimeout"
        )
    )
    assert "Authorization" not in caplog.text
    assert "private" not in caplog.text

    with caplog.at_level(
        logging.WARNING, logger="workgate.executor.connection"
    ):
        connection._report_status("connected")
        connection._report_status(
            "reconnecting",
            "ExecutorControlError",
            control_error=ExecutorControlError(
                "private bearer",
                transport_error="privateBearerOrHeaders",
            ),
        )
    assert caplog.records[-1].getMessage().endswith("transport=none")
    assert "privateBearerOrHeaders" not in caplog.text


def test_reconnect_warning_rejects_untrusted_request_kind(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection = ExecutorConnection(
        _ScriptedReconnectClient(()),
        hello_factory=_hello,
        execute=lambda _command: None,
        max_concurrent_commands=1,
    )
    with caplog.at_level(
        logging.WARNING, logger="workgate.executor.connection"
    ):
        connection._report_status(
            "reconnecting",
            "ExecutorControlError",
            control_error=ExecutorControlError(
                "private body",
                request_kind="secret\nAuthorization",
            ),
        )
    assert caplog.records[-1].getMessage() == (
        "executor reconnecting: ExecutorControlError "
        "request=unknown http=unknown code=none transport=none"
    )
    assert "Authorization" not in caplog.text


def test_connection_logging_failure_does_not_block_status_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workgate.executor import connection as connection_module

    observed: list[tuple[str, str | None]] = []

    def unavailable_logger(*_args: object) -> None:
        raise RuntimeError("logging sink failed")

    monkeypatch.setattr(connection_module.logger, "warning", unavailable_logger)
    connection = ExecutorConnection(
        _ScriptedReconnectClient(()),
        hello_factory=_hello,
        execute=lambda _command: None,
        max_concurrent_commands=1,
        status_callback=lambda phase, reason: observed.append((phase, reason)),
    )
    connection._report_status("reconnecting", "TimeoutError")
    assert observed == [("reconnecting", "TimeoutError")]


@pytest.mark.asyncio
async def test_connection_status_callback_tracks_retry_and_recovery() -> None:
    observed: list[tuple[str, str | None]] = []
    reached_poll = asyncio.Event()

    class Client(_BaseFakeClient):
        hello_calls = 0

        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            self.hello_calls += 1
            if self.hello_calls == 1:
                raise ConnectionResetError("transient")
            return _policy()

        async def heartbeat(self) -> None:
            return None

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            reached_poll.set()
            await asyncio.Event().wait()
            return None

        async def submit_result(self, result: ExecutorResult) -> None:
            raise AssertionError("no result expected")

    async def yielding_sleep(delay: float) -> None:
        if delay == 1.0:
            await asyncio.Event().wait()
        await asyncio.sleep(0)

    client = Client()
    connection = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=lambda command: None,
        max_concurrent_commands=1,
        sleep=yielding_sleep,
        random_value=lambda: 0.5,
        status_callback=lambda phase, reason: observed.append((phase, reason)),
    )
    connection.start()
    try:
        await asyncio.wait_for(reached_poll.wait(), timeout=0.5)
        assert observed[:3] == [
            ("reconnecting", None),
            ("reconnecting", "ConnectionResetError"),
            ("connected", None),
        ]
    finally:
        await connection.aclose()
    assert observed[-1] == ("connected", None)


@pytest.mark.asyncio
async def test_connection_status_preserves_revoked_credential_action() -> None:
    observed: list[tuple[str, str | None]] = []

    class Client(_BaseFakeClient):
        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            raise _protocol_error(
                ProtocolErrorCode.EXECUTOR_REVOKED, status=403
            )

        async def heartbeat(self) -> None:
            raise AssertionError("revoked executor must not heartbeat")

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            raise AssertionError("revoked executor must not poll")

        async def submit_result(self, result: ExecutorResult) -> None:
            raise AssertionError("no result expected")

    connection = ExecutorConnection(
        Client(),
        hello_factory=_hello,
        execute=lambda command: None,
        max_concurrent_commands=1,
        status_callback=lambda phase, reason: observed.append((phase, reason)),
    )
    connection.start()
    try:
        await asyncio.wait_for(connection.wait_owner_action(), timeout=0.5)
    finally:
        await connection.aclose()
    assert observed == [
        ("reconnecting", None),
        ("action-required", "executor_revoked"),
    ]


@pytest.mark.asyncio
async def test_status_callback_failure_cannot_disable_executor_connection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reached_poll = asyncio.Event()

    class Client(_BaseFakeClient):
        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            return _policy()

        async def heartbeat(self) -> None:
            return None

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            reached_poll.set()
            await asyncio.Event().wait()
            return None

        async def submit_result(self, result: ExecutorResult) -> None:
            raise AssertionError("no result expected")

    def bad_diagnostic(_phase: str, _reason: str | None) -> None:
        raise PermissionError("private status path and bearer secret")

    connection = ExecutorConnection(
        Client(),
        hello_factory=_hello,
        execute=lambda command: None,
        max_concurrent_commands=1,
        status_callback=bad_diagnostic,
    )
    connection.start()
    try:
        await asyncio.wait_for(reached_poll.wait(), timeout=0.5)
        assert connection.owner_action_error is None
    finally:
        await connection.aclose()
    assert (
        "executor connection status write failed: phase=connected error=PermissionError"
        in caplog.text
    )
    assert "private status path" not in caplog.text
    assert "bearer secret" not in caplog.text


@pytest.mark.asyncio
async def test_transient_runtime_policy_failure_rehellos_with_bounded_backoff() -> (
    None
):
    delays: list[float] = []
    resumed = asyncio.Event()

    class Client(_BaseFakeClient):
        hello_calls = 0
        poll_calls = 0

        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            self.hello_calls += 1
            return ExecutorHelloResponse(
                heartbeat_interval_s=10,
                offline_after_s=30,
                poll_timeout_s=1,
                required_workgate_version="5.0.0a99",
                runtime_update_required=self.hello_calls < 3,
            )

        async def heartbeat(self) -> None:
            return None

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            self.poll_calls += 1
            resumed.set()
            await asyncio.Event().wait()
            return None

        async def submit_result(self, result: ExecutorResult) -> None:
            raise AssertionError("no result expected")

    client = Client()

    async def policy_handler(
        policy: ExecutorHelloResponse,
    ) -> RuntimePolicyAction:
        if policy.runtime_update_required:
            raise ConnectionError("source release temporarily unavailable")
        return RuntimePolicyAction.CONTINUE

    async def retry_sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    connection = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=lambda _command: None,
        max_concurrent_commands=1,
        runtime_policy_handler=policy_handler,
        sleep=retry_sleep,
        random_value=lambda: 0.5,
    )
    connection.start()
    try:
        await asyncio.wait_for(resumed.wait(), timeout=1)
        assert client.hello_calls == 3
        assert client.poll_calls == 1
        assert delays[:2] == [0.5, 1.0]
        assert connection.owner_action_error is None
    finally:
        await connection.aclose()


@pytest.mark.asyncio
async def test_source_cutover_handoff_quiesces_without_owner_action() -> None:
    """A scheduled native switch cannot continue polling or claim success."""
    handed_off = asyncio.Event()
    phases: list[tuple[str, str | None]] = []

    class Client(_BaseFakeClient):
        async def hello(
            self, message: ExecutorHelloRequest
        ) -> ExecutorHelloResponse:
            return ExecutorHelloResponse(
                heartbeat_interval_s=10,
                offline_after_s=30,
                poll_timeout_s=1,
                required_workgate_version="5.0.0a99",
                runtime_update_required=True,
            )

        async def heartbeat(self) -> None:
            raise AssertionError("source cutover cannot heartbeat")

        async def poll(self, *, timeout_s: float) -> ExecutorCommand | None:
            raise AssertionError("source cutover cannot accept new work")

        async def submit_result(self, result: ExecutorResult) -> None:
            raise AssertionError("no command results expected")

    async def runtime_policy(
        _policy: ExecutorHelloResponse,
    ) -> RuntimePolicyAction:
        handed_off.set()
        return RuntimePolicyAction.HANDOFF

    connection = ExecutorConnection(
        Client(),
        hello_factory=_hello,
        execute=lambda _command: None,
        max_concurrent_commands=1,
        runtime_policy_handler=runtime_policy,
        status_callback=lambda phase, error: phases.append((phase, error)),
    )
    connection.start()
    try:
        await asyncio.wait_for(handed_off.wait(), timeout=1)
        await asyncio.sleep(0)
        assert ("reconnecting", "RuntimeCutoverPending") in phases
        assert connection.restart_requested is False
        assert connection.owner_action_error is None
        assert not connection._first_hello.is_set()
    finally:
        await connection.aclose()
