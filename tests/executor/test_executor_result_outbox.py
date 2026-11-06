"""Durable Executor result delivery must never monopolize command execution lanes."""

import asyncio
import os
from pathlib import Path

import pytest

from tests.executor.test_executor_connection import _hello, _policy
from workgate.executor.connection import ExecutorConnection
from workgate.executor.control_client import ExecutorControlError
from workgate.executor.profile import ExecutorProfile
from workgate.executor.result_outbox import (
    ExecutorResultOutbox,
    ResultOutboxFullError,
)
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.errors import ProtocolError, ProtocolErrorCode
from workgate.protocol.executor import ExecutorCommand, ExecutorResult
from workgate.protocol.ids import new_command_id, new_executor_id


def _profile() -> ExecutorProfile:
    return ExecutorProfile(
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
        control_url="http://localhost:8765",
    )


def _result() -> ExecutorResult:
    return ExecutorResult(
        id=new_command_id(), ok=True, result={"payload": "secret-data"}
    )


def test_private_persisted_results_and_profile_isolation(tmp_path: Path):
    profile = _profile()
    store = ExecutorResultOutbox(tmp_path, profile)
    result = _result()
    path = store.enqueue(result)
    assert store.count() == 1
    assert store.oldest() == path
    assert store.read(path) == result
    assert b"secret-data" in path.read_bytes()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    changed_profile = profile.model_copy(
        update={"credential": new_executor_credential()}
    )
    other = ExecutorResultOutbox(tmp_path, changed_profile)
    assert other.count() == 0
    store.remove(path)
    assert store.count() == 0


def test_bounded_outbox_never_overwrites_existing_result(tmp_path, monkeypatch):
    from workgate.executor import result_outbox

    profile = _profile()
    store = ExecutorResultOutbox(tmp_path, profile)
    monkeypatch.setattr(result_outbox, "_MAX_RESULTS", 1)
    first = store.enqueue(_result())
    with pytest.raises(ResultOutboxFullError, match="full"):
        store.enqueue(_result())
    assert store.count() == 1
    assert first.is_file()
    store.remove(first)
    monkeypatch.setattr(result_outbox, "_MAX_BYTES", 1)
    with pytest.raises(ResultOutboxFullError, match="byte budget"):
        store.enqueue(_result())
    assert store.count() == 0


class _FakeClient:
    def __init__(
        self, commands: list[ExecutorCommand], *, blocked: bool = False
    ):
        self.commands = list(commands)
        self.blocked = blocked
        self.upload_started = asyncio.Event()
        self.release = asyncio.Event()
        self.accepted: list[ExecutorResult] = []
        self.closed = False

    async def hello(self, message):
        return _policy()

    async def heartbeat(self):
        await asyncio.Event().wait()

    async def poll(self, *, timeout_s):
        if self.commands:
            return self.commands.pop(0)
        await asyncio.Event().wait()

    async def submit_result(self, result: ExecutorResult):
        self.upload_started.set()
        if self.blocked:
            await self.release.wait()
        self.accepted.append(result)

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_four_blocked_result_submissions_do_not_block_fifth_command(
    tmp_path,
):
    profile = _profile()
    store = ExecutorResultOutbox(tmp_path, profile)
    commands = [
        ExecutorCommand(id=new_command_id(), op="test.echo") for _ in range(5)
    ]
    client = _FakeClient(commands, blocked=True)
    executed: list[str] = []
    done = asyncio.Event()

    def execute(command):
        executed.append(command.id)
        if len(executed) == 5:
            done.set()
        return {"ok": True}

    connection = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=execute,
        max_concurrent_commands=4,
        result_outbox=store,
    )
    connection.start()
    try:
        await asyncio.wait_for(client.upload_started.wait(), timeout=2)
        await asyncio.wait_for(done.wait(), timeout=2)
        # All five operations executed even though the first ACK never arrived.
        async with asyncio.timeout(2):
            while store.count() != 5 or connection.active_command_count:
                await asyncio.sleep(0.01)
        assert len(executed) == 5
        assert client.accepted == []
    finally:
        await connection.aclose()
    assert store.count() == 5

    # A different connection instance using the same paired profile replays
    # the completed results without running the underlying commands again.
    restored = _FakeClient([])
    new_connection = ExecutorConnection(
        restored,
        hello_factory=_hello,
        execute=lambda _command: pytest.fail(
            "spooled commands must never rerun"
        ),
        max_concurrent_commands=4,
        result_outbox=ExecutorResultOutbox(tmp_path, profile),
    )
    new_connection.start()
    try:
        async with asyncio.timeout(3):
            while len(restored.accepted) < 5:
                await asyncio.sleep(0.01)
        assert {item.id for item in restored.accepted} == {
            cmd.id for cmd in commands
        }
        assert store.count() == 0
    finally:
        await new_connection.aclose()


@pytest.mark.asyncio
async def test_terminal_unknown_command_discards_stale_replayed_result(
    tmp_path,
):
    store = ExecutorResultOutbox(tmp_path, _profile())
    store.enqueue(_result())

    class UnknownClient(_FakeClient):
        async def submit_result(self, result):
            self.upload_started.set()
            raise ExecutorControlError(
                "already completed or expired",
                status_code=404,
                protocol_error=ProtocolError(
                    code=ProtocolErrorCode.UNKNOWN_COMMAND,
                    message="already done",
                ),
            )

    client = UnknownClient([])
    connection = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=lambda _command: None,
        max_concurrent_commands=1,
        result_outbox=store,
    )
    connection.start()
    try:
        await asyncio.wait_for(client.upload_started.wait(), timeout=2)
        async with asyncio.timeout(2):
            while store.count() != 0:
                await asyncio.sleep(0.01)
    finally:
        await connection.aclose()


@pytest.mark.asyncio
async def test_disk_capacity_failure_keeps_direct_result_submission(
    tmp_path, monkeypatch
):
    from workgate.executor import result_outbox

    store = ExecutorResultOutbox(tmp_path, _profile())
    monkeypatch.setattr(result_outbox, "_MAX_RESULTS", 0)
    client = _FakeClient([])
    conn = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=lambda _command: {"ok": True},
        max_concurrent_commands=1,
        result_outbox=store,
    )
    cmd = ExecutorCommand(id=new_command_id(), op="test.echo")
    await conn._run_command(cmd, inventory_completion=None)
    assert len(client.accepted) == 1 and client.accepted[0].id == cmd.id
    assert store.count() == 0


@pytest.mark.asyncio
async def test_corrupt_replayed_entry_is_quarantined_and_does_not_block_later_results(
    tmp_path,
):
    store = ExecutorResultOutbox(tmp_path, _profile())
    damaged = store.enqueue(_result())
    damaged.write_text("not-json", encoding="utf-8")
    valid = _result()
    store.enqueue(valid)
    client = _FakeClient([])
    conn = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=lambda _command: None,
        max_concurrent_commands=1,
        result_outbox=store,
    )
    conn.start()
    try:
        async with asyncio.timeout(3):
            while len(client.accepted) != 1:
                await asyncio.sleep(0.01)
        assert client.accepted == [valid]
        assert store.count() == 0
        assert damaged.with_suffix(".invalid").exists()
    finally:
        await conn.aclose()


@pytest.mark.asyncio
async def test_result_persists_after_command_cancel_during_disk_write(
    tmp_path, monkeypatch
):
    import threading

    store = ExecutorResultOutbox(tmp_path, _profile())
    original_enqueue = store.enqueue
    entered = threading.Event()
    release = threading.Event()

    def slow_write(result):
        entered.set()
        assert release.wait(timeout=3)
        return original_enqueue(result)

    monkeypatch.setattr(store, "enqueue", slow_write)
    client = _FakeClient([])
    conn = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=lambda _command: {"done": True},
        max_concurrent_commands=1,
        result_outbox=store,
    )
    command = ExecutorCommand(id=new_command_id(), op="test.echo")
    sender = asyncio.create_task(conn._result_outbox_sender())
    running = asyncio.create_task(
        conn._run_command(command, inventory_completion=None)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        release.set()
        await asyncio.wait_for(client.upload_started.wait(), timeout=2)
        async with asyncio.timeout(2):
            while store.count():
                await asyncio.sleep(0.01)
        assert len(client.accepted) == 1
        assert client.accepted[0].id == command.id
    finally:
        release.set()
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)


@pytest.mark.asyncio
async def test_lost_ack_looks_like_unknown_command_but_never_reexecutes(
    tmp_path,
):
    store = ExecutorResultOutbox(tmp_path, _profile())
    saved = _result()
    store.enqueue(saved)

    class LostAckClient(_FakeClient):
        async def submit_result(self, result):
            self.upload_started.set()
            self.accepted.append(result)
            if len(self.accepted) == 1:
                raise ExecutorControlError("response was lost")
            raise ExecutorControlError(
                "control already committed result",
                status_code=409,
                protocol_error=ProtocolError(
                    code=ProtocolErrorCode.UNKNOWN_COMMAND,
                    message="already done",
                ),
            )

    client = LostAckClient([])

    async def no_wait(_delay):
        await asyncio.sleep(0)

    conn = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=lambda _command: pytest.fail("must not rerun"),
        max_concurrent_commands=1,
        result_outbox=store,
        sleep=no_wait,
    )
    conn.start()
    try:
        async with asyncio.timeout(2):
            while store.count() or len(client.accepted) < 2:
                await asyncio.sleep(0.01)
        assert client.accepted == [saved, saved]
    finally:
        await conn.aclose()


@pytest.mark.asyncio
async def test_config_apply_keeps_ack_before_restart_path(tmp_path):
    store = ExecutorResultOutbox(tmp_path, _profile())
    client = _FakeClient([])
    conn = ExecutorConnection(
        client,
        hello_factory=_hello,
        execute=lambda _command: {"status": "unchanged"},
        max_concurrent_commands=1,
        result_outbox=store,
    )
    command = ExecutorCommand(id=new_command_id(), op="ui.config.apply")
    await conn._run_command(command, inventory_completion=None)
    assert [result.id for result in client.accepted] == [command.id]
    assert store.count() == 0


def test_quarantined_corrupt_results_remain_inside_total_storage_budget(
    tmp_path, monkeypatch
):
    from workgate.executor import result_outbox

    store = ExecutorResultOutbox(tmp_path, _profile())
    monkeypatch.setattr(result_outbox, "_MAX_RESULTS", 1)
    broken = store.enqueue(_result())
    store.quarantine(broken)
    assert store.count() == 0
    assert broken.with_suffix(".invalid").exists()
    with pytest.raises(ResultOutboxFullError, match="full"):
        store.enqueue(_result())
