"""Paired native Task/Session creation with durable intent reconciliation."""

import asyncio
import json
import shutil
import time

import pytest

from tests.support.paired import build_paired_control_harness
from workgate.audit import query_audit
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.execution_context import control_execution_context
from workgate.control.executor_transport import ExecutorTransportError
from workgate.control.hosted_chat_cancellation import sweep_native_cancellations
from workgate.control.hosted_chat_job_inspection import inspect_native_jobs
from workgate.control.hosted_chat_native_creation import (
    NativeCreationUncertain,
    NativeRetryRejected,
    create_native,
)
from workgate.executor import shell as shell_ops
from workgate.executor import shell_service
from workgate.executor.jobs import shell as job_shell
from workgate.hosted_chat.database import SQLiteDatabase
from workgate.hosted_chat.durable_tasks import DurableTaskRepository
from workgate.hosted_chat.generation_actor_lease import ActorLease
from workgate.hosted_chat.generations import GenerationStore
from workgate.hosted_chat.provider_common import ModelToolCall
from workgate.hosted_chat.storage import WorkspaceRepository
from workgate.oauth.core.context import bind_oauth_claims, reset_oauth_claims
from workgate.protocol.errors import ProtocolErrorCode
from workgate.protocol.executor import (
    JOB_LOOKUP_ATTEMPT_OP,
    JOB_RETRY_RESERVED_OP,
    JOB_START_RESERVED_OP,
    SESSION_CREATE_OP,
    SESSION_LOOKUP_OP,
    SHELL_LOOKUP_RESERVED_OP,
    SHELL_START_RESERVED_OP,
)
from workgate.schemas.result_models.shell import (
    KillPersistentShellOutput,
    ReadPersistentShellOutput,
    StartPersistentShellOutput,
)

SCOPES = {"shell:read", "shell:write", "shell:execute"}


@pytest.fixture
def bound(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    clear_settings_cache()
    (tmp_path / "project").mkdir()
    harness = build_paired_control_harness(get_settings())
    database = SQLiteDatabase(harness.control.config.state_dir)
    generation, _ = GenerationStore(database).begin(
        "local-user", {"text": "native work"}
    )
    lease = ActorLease.claim(database, "local-user", generation.run_id)
    assert lease is not None
    yield harness, DurableTaskRepository(database), lease, generation
    clear_settings_cache()


async def invoke(harness, repo, lease, call, *, scopes=SCOPES):
    return await create_native(
        harness.control,
        repo,
        lease,
        call,
        machine=harness.executor_id,
        scopes=scopes,
        expires=None,
    )


@pytest.mark.asyncio
async def test_native_creation_recovers_lost_response_without_reoffering(
    bound, monkeypatch
):
    harness, repo, lease, generation = bound
    runtime = harness.control
    proposal = ModelToolCall(
        "task-call", "task", {"action": "create", "label": "native task"}
    )
    first = await invoke(harness, repo, lease, proposal)
    task_id = first["task_id"]
    assert (await invoke(harness, repo, lease, proposal))["task_id"] == task_id
    assert len(await runtime.task_service.list_tasks()) == 1

    transport = runtime.executor_transport
    original = transport.call
    offered = []
    lookup_allowed = False

    async def lost_response(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        if op == SESSION_LOOKUP_OP and not lookup_allowed:
            raise TimeoutError("offline")
        response = await original(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )
        if op == SESSION_CREATE_OP:
            offered.append(session_id)
            raise ExecutorTransportError(
                ProtocolErrorCode.EXECUTOR_OFFLINE,
                "lost response",
                delivery_state="offered",
            )
        return response

    monkeypatch.setattr(transport, "call", lost_response)
    session = ModelToolCall(
        "session-call",
        "session_start",
        {
            "executor_id": harness.executor_id,
            "task_id": task_id,
            "workdir": "project",
        },
    )
    with pytest.raises(ExecutorTransportError, match="lost response"):
        await invoke(harness, repo, lease, session)
    with pytest.raises(NativeCreationUncertain):
        await invoke(harness, repo, lease, session)
    assert len(offered) == 1
    assert not repo.owned(
        "local-user",
        generation.conversation_id,
        "session",
        offered[0],
        machine=harness.executor_id,
    )
    lookup_allowed = True
    recovered = await invoke(harness, repo, lease, session)
    assert recovered["recovered"] and recovered["session_id"] == offered[0]
    assert (
        recovered["executor_id"] == harness.executor_id
        and recovered["task_id"] == task_id
    )
    assert "environment" not in recovered
    assert (
        len(offered) == 1
        and len(runtime.control_state.snapshot_sessions()) == 1
    )
    assert repo.owned(
        "local-user",
        generation.conversation_id,
        "session",
        recovered["session_id"],
        machine=harness.executor_id,
    )
    with control_execution_context(
        config=runtime.config, state_store=runtime.state_store
    ):
        entries = query_audit(event="tool_call_end", limit=100)["entries"]
    assert any(
        row.get("tool") == "session_start"
        and row.get("session") == recovered["session_id"]
        and row.get("paired") is True
        and row.get("status") == "success"
        for row in entries
    )


@pytest.mark.asyncio
async def test_native_grant_and_principal_cannot_be_widened(bound):
    harness, repo, lease, _ = bound
    proposal = ModelToolCall("task-call", "task", {"action": "create"})
    token = bind_oauth_claims(
        {
            "sub": "other",
            "scope": "shell:read shell:write",
            "exp": int(time.time()) + 120,
        }
    )
    try:
        with pytest.raises(PermissionError, match="principal"):
            await invoke(harness, repo, lease, proposal)
    finally:
        reset_oauth_claims(token)
    token = bind_oauth_claims(
        {
            "sub": "local-user",
            "scope": "shell:read",
            "exp": int(time.time()) + 120,
        }
    )
    try:
        with pytest.raises(PermissionError, match="shell:write"):
            await invoke(harness, repo, lease, proposal)
    finally:
        reset_oauth_claims(token)
    with repo.database.connect() as db:
        assert (
            db.execute("SELECT count(*) FROM agent_durable_tasks").fetchone()[0]
            == 0
        )
    assert await harness.control.task_service.list_tasks() == []


@pytest.mark.asyncio
async def test_cancel_racing_creation_retains_native_receipt_without_disclosure(
    bound, monkeypatch
):
    harness, repo, lease, generation = bound
    service = harness.control.task_service
    create = service.create_task

    async def create_then_cancel(**kwargs):
        result = await create(**kwargs)
        repo.cancel_run(lease.owner, lease.run_id)
        return result

    monkeypatch.setattr(service, "create_task", create_then_cancel)
    proposal = ModelToolCall("task-call", "task", {"action": "create"})
    with pytest.raises(PermissionError, match="cancelled"):
        await invoke(harness, repo, lease, proposal)
    tasks = await service.list_tasks()
    assert len(tasks) == 1
    with repo.database.connect() as db:
        row = db.execute(
            "SELECT native_id,state FROM agent_durable_tasks WHERE run_id=?",
            (generation.run_id,),
        ).fetchone()
        assert (
            row["state"] == "cancel_pending"
            and row["native_id"] == tasks[0].task_id
        )
    with pytest.raises(PermissionError):
        await invoke(harness, repo, lease, proposal)
    assert len(await service.list_tasks()) == 1


@pytest.mark.asyncio
async def test_native_creation_confirmed_normal_session_and_replay(bound):
    harness, repo, lease, _ = bound
    task = await invoke(
        harness,
        repo,
        lease,
        ModelToolCall("task", "task", {"action": "create"}),
    )
    call = ModelToolCall(
        "session",
        "session_start",
        {
            "executor_id": harness.executor_id,
            "task_id": task["task_id"],
            "workdir": "project",
        },
    )
    first = await invoke(harness, repo, lease, call)
    again = await invoke(harness, repo, lease, call)
    assert first["session_id"] == again["session_id"]
    assert again["recovered"] is True
    assert len(harness.control.control_state.snapshot_sessions()) == 1


@pytest.mark.asyncio
async def test_native_creation_missing_task_receipt_never_reoffers(
    bound, monkeypatch
):
    harness, repo, lease, _ = bound
    attempts = []

    async def unavailable(**kwargs):
        attempts.append(kwargs)
        raise TimeoutError("receipt unavailable")

    monkeypatch.setattr(
        harness.control.task_service, "create_task", unavailable
    )
    call = ModelToolCall("task", "task", {"action": "create"})
    with pytest.raises(TimeoutError):
        await invoke(harness, repo, lease, call)
    with pytest.raises(NativeCreationUncertain):
        await invoke(harness, repo, lease, call)
    assert len(attempts) == 1
    assert not await harness.control.task_service.list_tasks()


@pytest.fixture
def tracked_shells(monkeypatch):
    active = set()
    started = []
    killed = []

    async def start(
        _config, _store, cwd, name, command, *, owner_session_id=None
    ):
        started.append(name)
        active.add(name)
        return StartPersistentShellOutput(
            shell_id=name, name=name, cwd=cwd, command=command, backend="fake"
        )

    async def inventory(*args):
        return set(active)

    async def read(_config, shell_id, lines):
        return ReadPersistentShellOutput(
            shell_id=shell_id, output="native output", lines=lines
        )

    async def kill(_config, _store, shell_id):
        killed.append(shell_id)
        active.discard(shell_id)
        return KillPersistentShellOutput(shell_id=shell_id, killed=True)

    monkeypatch.setattr(job_shell, "start_persistent_shell_execute", start)
    monkeypatch.setattr(
        job_shell, "authoritative_persistent_shell_ids_execute", inventory
    )
    monkeypatch.setattr(job_shell, "read_persistent_shell_output_execute", read)
    monkeypatch.setattr(job_shell, "kill_persistent_shell_execute", kill)
    return started, killed


async def native_session(harness, repo, lease):
    task = await invoke(
        harness,
        repo,
        lease,
        ModelToolCall("native-task", "task", {"action": "create"}),
    )
    session = await invoke(
        harness,
        repo,
        lease,
        ModelToolCall(
            "native-session",
            "session_start",
            {"executor_id": harness.executor_id, "task_id": task["task_id"]},
        ),
    )
    return task["task_id"], session["session_id"]


def intent_rows(repo):
    with repo.database.connect() as db:
        return {
            row["call_id"]: dict(row)
            for row in db.execute("SELECT * FROM agent_durable_tasks")
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,body",
    [
        ("bash", {"command": "echo hello"}),
        ("run_python_code", {"code": "print('hello')"}),
    ],
)
async def test_tracked_job_lost_response_reconciles_exact_session_and_id(
    bound, tracked_shells, monkeypatch, tool, body
):
    harness, repo, lease, generation = bound
    started, _ = tracked_shells
    _, session_id = await native_session(harness, repo, lease)
    transport = harness.control.executor_transport
    original = transport.call
    offered = []

    async def lost_response(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        result = await original(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )
        if op == JOB_START_RESERVED_OP:
            assert result.ok and args is not None
            offered.append((executor_id, session_id, args["job_id"]))
            raise ExecutorTransportError(
                ProtocolErrorCode.EXECUTOR_OFFLINE,
                "lost Job response",
                delivery_state="offered",
            )
        return result

    monkeypatch.setattr(transport, "call", lost_response)
    call = ModelToolCall(
        "tracked", tool, {"session_id": session_id, "async_": True, **body}
    )
    with pytest.raises(ExecutorTransportError, match="lost Job response"):
        await invoke(harness, repo, lease, call)
    row = intent_rows(repo)["tracked"]
    assert row["state"] == "unknown" and row["native_id"] is None
    assert row["native_session_id"] == session_id
    assert offered == [
        (harness.executor_id, session_id, row["idempotency_key"])
    ]
    result = await invoke(harness, repo, lease, call)
    assert result["recovered"] and result["mode"] == "job"
    assert result["result"]["job_id"] == row["idempotency_key"]
    assert result["result"]["stdout"] == "native output"
    assert len(offered) == 1 and len(started) == 1
    assert repo.owned(
        "local-user",
        generation.conversation_id,
        "job",
        row["idempotency_key"],
        machine=harness.executor_id,
        session_id=session_id,
    )
    assert not repo.owned(
        "local-user",
        generation.conversation_id,
        "job",
        row["idempotency_key"],
        machine=harness.executor_id,
    )
    assert not repo.owned(
        "local-user",
        generation.conversation_id,
        "job",
        row["idempotency_key"],
        machine=harness.executor_id,
        session_id="wrong",
    )


@pytest.mark.asyncio
async def test_tracked_job_checks_execute_scope_and_native_session_ownership(
    bound, tracked_shells
):
    harness, repo, lease, generation = bound
    started, _ = tracked_shells
    _, session_id = await native_session(harness, repo, lease)
    call = ModelToolCall(
        "tracked",
        "bash",
        {"session_id": session_id, "command": "echo hello", "async_": True},
    )
    for scopes in (
        {"shell:read", "shell:execute"},
        {"shell:read", "shell:write"},
    ):
        with pytest.raises(PermissionError):
            await invoke(harness, repo, lease, call, scopes=scopes)
    workspace = WorkspaceRepository(repo.database)
    key = "agent.machine_policy.conversation." + generation.conversation_id
    workspace.save_setting("local-user", key, {harness.executor_id: "read"})
    with pytest.raises(PermissionError, match="read-only"):
        await invoke(harness, repo, lease, call)
    workspace.save_setting("local-user", key, {})
    foreign, _ = GenerationStore(repo.database).begin(
        "local-user", {"text": "foreign"}
    )
    foreign_lease = ActorLease.claim(
        repo.database, "local-user", foreign.run_id
    )
    assert foreign_lease is not None
    with pytest.raises(PermissionError, match="does not belong"):
        await invoke(harness, repo, foreign_lease, call)
    for extra in (
        {"job_id": "injected"},
        {"pty": True},
        {"async_": False},
        {"idempotency_key": "injected"},
    ):
        with pytest.raises(ValueError):
            await invoke(
                harness,
                repo,
                lease,
                ModelToolCall("tracked", "bash", call.arguments | extra),
            )
    assert not started
    assert "tracked" not in intent_rows(repo)


@pytest.mark.asyncio
async def test_abort_tracked_job_in_reused_session_does_not_end_session(
    bound, tracked_shells, monkeypatch
):
    harness, repo, lease, generation = bound
    started, killed = tracked_shells
    task_id, session_id = await native_session(harness, repo, lease)
    store = GenerationStore(repo.database)
    assert store.finish(
        lease.owner,
        generation.run_id,
        status="complete",
        text="ready",
        actor_lease=lease,
    )
    next_run, _ = store.begin(
        lease.owner,
        {"conversationId": generation.conversation_id, "text": "run Job"},
    )
    next_lease = ActorLease.claim(repo.database, lease.owner, next_run.run_id)
    assert next_lease is not None
    transport = harness.control.executor_transport
    original = transport.call

    async def lost_response(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        result = await original(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )
        if op == JOB_START_RESERVED_OP:
            raise ExecutorTransportError(
                ProtocolErrorCode.EXECUTOR_OFFLINE,
                "lost Job response",
                delivery_state="offered",
            )
        return result

    monkeypatch.setattr(transport, "call", lost_response)
    call = ModelToolCall(
        "tracked",
        "bash",
        {"session_id": session_id, "command": "long running", "async_": True},
    )
    with pytest.raises(ExecutorTransportError):
        await invoke(harness, repo, next_lease, call)
    assert intent_rows(repo)["tracked"]["native_id"] is None
    repo.cancel_run(next_lease.owner, next_run.run_id)
    assert await sweep_native_cancellations(harness.control, repo) == 1
    row = intent_rows(repo)["tracked"]
    assert (
        row["state"] == "stopped" and row["native_id"] == row["idempotency_key"]
    )
    assert killed == started and len(killed) == 1
    assert harness.executor.sessions.lookup(session_id) is not None
    assert (
        await harness.control.task_service.read_task(task_id)
    ).status == "active"
    assert intent_rows(repo)["native-session"]["state"] == "active"


@pytest.mark.asyncio
async def test_cancel_unconfirmed_job_after_session_absence_does_not_fabricate_receipt(
    bound,
):
    harness, repo, lease, _ = bound
    _, session_id = await native_session(harness, repo, lease)
    proposal = ModelToolCall(
        "tracked",
        "bash",
        {"session_id": session_id, "command": "never offered", "async_": True},
    )
    grant = {
        "scopes": SCOPES,
        "expires": None,
        "registered": (harness.executor_id,),
    }
    intent = repo.reserve(lease, proposal, machine=harness.executor_id, **grant)
    assert repo.claim(lease, intent, proposal, **grant)
    await harness.control.session_coordinator.end_session(session_id)
    repo.cancel_run(lease.owner, lease.run_id)
    await sweep_native_cancellations(harness.control, repo)
    row = intent_rows(repo)["tracked"]
    assert row["state"] == "stopped"
    assert row["native_id"] is None


@pytest.mark.asyncio
async def test_missing_tracked_job_receipt_never_reoffers(
    bound, tracked_shells, monkeypatch
):
    harness, repo, lease, _ = bound
    started, _ = tracked_shells
    _, session_id = await native_session(harness, repo, lease)
    transport = harness.control.executor_transport
    original = transport.call
    offered = []

    async def missing(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        if op == JOB_START_RESERVED_OP:
            offered.append(session_id)
            raise TimeoutError("Job response unknown")
        return await original(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )

    monkeypatch.setattr(transport, "call", missing)
    call = ModelToolCall(
        "missing-job",
        "bash",
        {"session_id": session_id, "command": "never observed", "async_": True},
    )
    with pytest.raises(TimeoutError):
        await invoke(harness, repo, lease, call)
    for _ in range(2):
        with pytest.raises(NativeCreationUncertain, match="unconfirmed"):
            await invoke(harness, repo, lease, call)
    row = intent_rows(repo)["missing-job"]
    assert row["state"] == "unknown" and row["native_id"] is None
    assert offered == [session_id] and not started


@pytest.fixture
def reserved_shell_backend(monkeypatch):
    started = {}
    killed = []

    async def start(
        _config,
        _store,
        cwd,
        name,
        command,
        *,
        owner_session_id=None,
        shell_id=None,
        forbidden_shell_ids=frozenset(),
    ):
        assert shell_id is not None and owner_session_id is not None
        assert shell_id not in started and shell_id not in forbidden_shell_ids
        started[shell_id] = owner_session_id
        return StartPersistentShellOutput(
            shell_id=shell_id, cwd=cwd, command=command, backend="fake"
        )

    async def owned(_config, _store, session_id):
        return [key for key, owner in started.items() if owner == session_id]

    async def kill(_config, store, shell_id):
        assert shell_id in started
        killed.append(shell_id)
        del started[shell_id]
        store.release_persistent_shell(shell_id)
        return KillPersistentShellOutput(
            shell_id=shell_id, killed=True, backend="fake"
        )

    monkeypatch.setattr(shell_ops, "_start_persistent_shell_locked", start)
    monkeypatch.setattr(
        shell_service, "list_owned_persistent_shell_ids_execute", owned
    )
    monkeypatch.setattr(shell_service, "kill_persistent_shell_execute", kill)
    return started, killed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,body",
    [
        ("bash", {"command": "shell"}),
        ("run_python_code", {"code": "print('hello')"}),
    ],
)
async def test_reserved_shell_lost_response_recovers_exact_owner(
    bound, reserved_shell_backend, monkeypatch, tool, body
):
    harness, repo, lease, generation = bound
    started, _ = reserved_shell_backend
    _, sid = await native_session(harness, repo, lease)
    transport = harness.control.executor_transport
    original = transport.call
    offers = []

    async def lost(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        result = await original(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )
        if op == SHELL_START_RESERVED_OP:
            assert result.ok and args is not None
            offers.append(args["shell_id"])
            raise TimeoutError("Shell response lost")
        return result

    monkeypatch.setattr(transport, "call", lost)
    call = ModelToolCall(
        "pty",
        tool,
        {"session_id": sid, "pty": True, "name": "my terminal", **body},
    )
    with pytest.raises(TimeoutError):
        await invoke(harness, repo, lease, call)
    result = await invoke(harness, repo, lease, call)
    key = result["result"]["shell_id"]
    assert key.startswith("chat-shell-my-terminal-")
    assert (
        result["recovered"] is True
        and offers == [key]
        and started == {key: sid}
    )
    assert repo.owned(
        lease.owner,
        generation.conversation_id,
        "shell",
        key,
        machine=harness.executor_id,
        session_id=sid,
    )
    assert not repo.owned(
        lease.owner,
        generation.conversation_id,
        "shell",
        key,
        machine=harness.executor_id,
    )
    other = await harness.control.session_coordinator.start_session(
        executor_id=harness.executor_id
    )
    foreign = await harness.call(
        harness.executor_id,
        SHELL_LOOKUP_RESERVED_OP,
        {"shell_id": key},
        session_id=other["session_id"],
    )
    assert foreign.ok and foreign.result["present"] is False
    duplicate = await harness.call(
        harness.executor_id,
        SHELL_START_RESERVED_OP,
        {"shell_id": key, "tool": "bash", "command": "duplicate"},
        session_id=sid,
    )
    assert not duplicate.ok
    spoof = await harness.call(
        harness.executor_id,
        "bash",
        {"pty": True, "command": "spoof", "name": key},
        session_id=other["session_id"],
    )
    assert not spoof.ok and started == {key: sid}


@pytest.mark.asyncio
async def test_reserved_shell_missing_receipt_and_invalid_mode_never_start(
    bound, reserved_shell_backend, monkeypatch
):
    harness, repo, lease, _ = bound
    started, _ = reserved_shell_backend
    _, sid = await native_session(harness, repo, lease)
    args = {"session_id": sid, "pty": True, "command": "never starts"}
    for extra in (
        {"async_": True},
        {"persist_on_timeout": True},
        {"shell_id": "forged"},
    ):
        with pytest.raises(ValueError):
            await invoke(
                harness,
                repo,
                lease,
                ModelToolCall("invalid", "bash", args | extra),
            )
    with pytest.raises(PermissionError):
        await invoke(
            harness,
            repo,
            lease,
            ModelToolCall("no-execute", "bash", args),
            scopes={"shell:read", "shell:write"},
        )
    original = harness.control.executor_transport.call
    offers = []

    async def missing(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        if op == SHELL_START_RESERVED_OP:
            offers.append(session_id)
            raise TimeoutError("Shell response lost")
        return await original(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )

    monkeypatch.setattr(harness.control.executor_transport, "call", missing)
    call = ModelToolCall("missing-pty", "bash", args)
    with pytest.raises(TimeoutError):
        await invoke(harness, repo, lease, call)
    with pytest.raises(NativeCreationUncertain):
        await invoke(harness, repo, lease, call)
    assert (
        offers == [sid]
        and not started
        and intent_rows(repo)["missing-pty"]["native_id"] is None
    )


@pytest.mark.asyncio
async def test_shell_cleanup_lost_kill_receipt_keeps_reused_session(
    bound, reserved_shell_backend, monkeypatch
):
    harness, repo, lease, generation = bound
    started, killed = reserved_shell_backend
    task_id, sid = await native_session(harness, repo, lease)
    store = GenerationStore(repo.database)
    assert store.finish(
        lease.owner,
        generation.run_id,
        status="complete",
        text="ready",
        actor_lease=lease,
    )
    next_run, _ = store.begin(
        lease.owner,
        {"text": "next", "conversationId": generation.conversation_id},
    )
    next_lease = ActorLease.claim(repo.database, lease.owner, next_run.run_id)
    assert next_lease is not None
    original = harness.control.executor_transport.call

    async def lost(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        result = await original(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )
        if op in {SHELL_START_RESERVED_OP, "kill_persistent_shell"}:
            assert result.ok
            raise TimeoutError("Shell mutation response lost")
        return result

    monkeypatch.setattr(harness.control.executor_transport, "call", lost)
    call = ModelToolCall(
        "cancel-pty",
        "bash",
        {"session_id": sid, "pty": True, "command": "shell"},
    )
    with pytest.raises(TimeoutError):
        await invoke(harness, repo, next_lease, call)
    key = next(iter(started))
    repo.cancel_run(next_lease.owner, next_run.run_id)
    assert await sweep_native_cancellations(harness.control, repo) == 0
    row = intent_rows(repo)["cancel-pty"]
    assert (
        row["state"] == "cancel_pending"
        and row["native_id"] == key
        and killed == [key]
        and not started
    )
    with repo.database.connect() as db:
        db.execute("UPDATE agent_durable_tasks SET next_attempt_at=NULL")
    assert await sweep_native_cancellations(harness.control, repo) == 1
    assert intent_rows(repo)["cancel-pty"]["state"] == "stopped"
    assert harness.executor.sessions.lookup(sid) is not None
    assert (
        await harness.control.task_service.read_task(task_id)
    ).status == "active"
    assert killed == [key]


async def next_generation(repo, lease, generation, text):
    store = GenerationStore(repo.database)
    assert store.finish(
        lease.owner,
        generation.run_id,
        status="complete",
        text="ready",
        actor_lease=lease,
    )
    run, _ = store.begin(
        lease.owner,
        {"conversationId": generation.conversation_id, "text": text},
    )
    actor = ActorLease.claim(repo.database, lease.owner, run.run_id)
    assert actor is not None
    return run, actor


async def create_tracked_job(harness, repo, lease, sid):
    proposal = ModelToolCall(
        "owned-job",
        "bash",
        {"session_id": sid, "command": "tracked", "async_": True},
    )
    result = await invoke(harness, repo, lease, proposal)
    return proposal, result["result"]["job_id"]


@pytest.mark.asyncio
async def test_retry_receipt_and_cleanup_are_fenced_by_attempt(
    bound, tracked_shells, monkeypatch
):
    harness, repo, lease, generation = bound
    started, killed = tracked_shells
    task_id, sid = await native_session(harness, repo, lease)
    first, first_lease = await next_generation(
        repo, lease, generation, "initial Job"
    )
    _, jid = await create_tracked_job(harness, repo, first_lease, sid)
    stopped = await harness.call(
        harness.executor_id, "job", {"cancel": [jid]}, session_id=sid
    )
    assert (
        stopped.ok
        and stopped.result["cancelled"][0]["job"]["status"] == "stopped"
    )
    second, second_lease = await next_generation(
        repo, first_lease, first, "retry Job"
    )
    transport = harness.control.executor_transport
    original = transport.call
    offered = []

    async def lost(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        result = await original(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )
        if op == JOB_RETRY_RESERVED_OP:
            assert result.ok and args is not None
            offered.append(dict(args))
            raise TimeoutError("retry response lost")
        return result

    monkeypatch.setattr(transport, "call", lost)
    retry = ModelToolCall(
        "retry-job", "job", {"session_id": sid, "retry": [jid]}
    )
    with pytest.raises(TimeoutError):
        await invoke(harness, repo, second_lease, retry)
    row = intent_rows(repo)["retry-job"]
    assert row["state"] == "unknown" and row["native_id"] is None
    assert row["target_job_id"] == jid and row["native_attempt"] == 2
    assert offered == [
        {"job_id": jid, "attempt": 2, "retry_key": row["idempotency_key"]}
    ]
    with pytest.raises(PermissionError, match="unresolved"):
        await invoke(
            harness,
            repo,
            second_lease,
            ModelToolCall("other-retry", "job", retry.arguments),
        )
    recovered = await invoke(harness, repo, second_lease, retry)
    assert recovered["recovered"] is True and recovered["operation"] == "retry"
    assert (
        recovered["retried"][0]["job_id"] == jid
        and recovered["retried"][0]["attempts"] == 2
    )
    assert len(started) == 2 and len(offered) == 1
    assert intent_rows(repo)["retry-job"]["native_id"] == jid

    duplicate = await harness.call(
        harness.executor_id, JOB_RETRY_RESERVED_OP, offered[0], session_id=sid
    )
    assert duplicate.ok and duplicate.result["state"] == "rejected"
    initial = await harness.call(
        harness.executor_id,
        JOB_LOOKUP_ATTEMPT_OP,
        {"job_id": jid, "attempt": 1},
        session_id=sid,
    )
    assert initial.ok and initial.result["state"] == "superseded"
    repo.cancel_run(first_lease.owner, first.run_id)
    assert await sweep_native_cancellations(harness.control, repo) == 1
    assert intent_rows(repo)["owned-job"]["state"] == "stopped"
    assert killed == [started[0]]
    assert (
        await harness.call(
            harness.executor_id, "job", {"poll": [jid]}, session_id=sid
        )
    ).result["outputs"][0]["job"]["status"] == "running"
    repo.cancel_run(second_lease.owner, second.run_id)
    assert await sweep_native_cancellations(harness.control, repo) == 1
    assert intent_rows(repo)["retry-job"]["state"] == "stopped"
    assert killed == started
    assert harness.executor.sessions.lookup(sid) is not None
    assert (
        await harness.control.task_service.read_task(task_id)
    ).status == "active"


@pytest.mark.asyncio
async def test_public_retry_does_not_become_chat_owned_attempt(
    bound, tracked_shells, monkeypatch
):
    harness, repo, lease, generation = bound
    started, killed = tracked_shells
    _, sid = await native_session(harness, repo, lease)
    first, first_lease = await next_generation(
        repo, lease, generation, "initial Job"
    )
    _, jid = await create_tracked_job(harness, repo, first_lease, sid)
    stopped = await harness.call(
        harness.executor_id, "job", {"cancel": [jid]}, session_id=sid
    )
    assert stopped.ok
    second, second_lease = await next_generation(
        repo, first_lease, first, "retry Job"
    )
    # A native client wins the race before Chat offers its reserved retry.
    native = await harness.call(
        harness.executor_id, "job", {"retry": [jid]}, session_id=sid
    )
    assert native.ok and native.result["retried"][0]["attempts"] == 2
    call = ModelToolCall(
        "retry-conflict", "job", {"session_id": sid, "retry": [jid]}
    )
    with pytest.raises(NativeRetryRejected):
        await invoke(harness, repo, second_lease, call)
    row = intent_rows(repo)["retry-conflict"]
    assert (
        row["state"] == "stopped"
        and row["native_id"] is None
        and row["native_attempt"] == 2
    )
    with pytest.raises(PermissionError):
        await invoke(harness, repo, second_lease, call)
    repo.cancel_run(second_lease.owner, second.run_id)
    assert await sweep_native_cancellations(harness.control, repo) == 0
    assert intent_rows(repo)["retry-conflict"]["state"] == "stopped"
    assert intent_rows(repo)["retry-conflict"]["native_id"] is None
    assert killed == [started[0]] and len(started) == 2
    assert (
        await harness.call(
            harness.executor_id, "job", {"poll": [jid]}, session_id=sid
        )
    ).result["outputs"][0]["job"]["status"] == "running"


@pytest.mark.asyncio
async def test_retry_requires_owned_job_and_live_execute_scope(
    bound, tracked_shells
):
    harness, repo, lease, _ = bound
    started, _ = tracked_shells
    _, sid = await native_session(harness, repo, lease)
    _, jid = await create_tracked_job(harness, repo, lease, sid)
    call = ModelToolCall("retry", "job", {"session_id": sid, "retry": [jid]})
    with pytest.raises(PermissionError):
        await invoke(
            harness, repo, lease, call, scopes={"shell:read", "shell:write"}
        )
    with pytest.raises(ValueError):
        await invoke(
            harness,
            repo,
            lease,
            ModelToolCall(
                "batch-retry", "job", {"session_id": sid, "retry": [jid, jid]}
            ),
        )
    from workgate.protocol.ids import new_reserved_job_id

    with pytest.raises(PermissionError, match="does not belong"):
        await invoke(
            harness,
            repo,
            lease,
            ModelToolCall(
                "foreign-retry",
                "job",
                {"session_id": sid, "retry": [new_reserved_job_id()]},
            ),
        )
    assert len(started) == 1 and "retry" not in intent_rows(repo)


@pytest.mark.asyncio
async def test_interrupted_native_retry_recovers_pending_attempt_receipt(
    bound, tracked_shells, monkeypatch
):
    harness, repo, lease, generation = bound
    started, killed = tracked_shells
    _, sid = await native_session(harness, repo, lease)
    first, first_lease = await next_generation(
        repo, lease, generation, "initial Job"
    )
    _, jid = await create_tracked_job(harness, repo, first_lease, sid)
    assert (
        await harness.call(
            harness.executor_id, "job", {"cancel": [jid]}, session_id=sid
        )
    ).ok
    second, second_lease = await next_generation(
        repo, first_lease, first, "retry Job"
    )
    start = job_shell.start_persistent_shell_execute

    async def interrupted(*args, **kwargs):
        await start(*args, **kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(
        job_shell, "start_persistent_shell_execute", interrupted
    )
    retry = ModelToolCall(
        "interrupted-retry", "job", {"session_id": sid, "retry": [jid]}
    )
    with pytest.raises(asyncio.CancelledError):
        await invoke(harness, repo, second_lease, retry)
    monkeypatch.setattr(job_shell, "start_persistent_shell_execute", start)
    row = intent_rows(repo)["interrupted-retry"]
    assert row["state"] == "unknown" and row["native_id"] is None
    recovered = await invoke(harness, repo, second_lease, retry)
    assert recovered["recovered"] is True
    assert recovered["retried"][0]["attempts"] == 2
    assert recovered["retried"][0]["status"] == "running"
    assert len(started) == 2
    repo.cancel_run(second_lease.owner, second.run_id)
    assert await sweep_native_cancellations(harness.control, repo) == 1
    assert killed == started


@pytest.mark.asyncio
async def test_explicit_retry_rejection_does_not_block_later_valid_retry(
    bound, tracked_shells
):
    harness, repo, lease, _ = bound
    started, _ = tracked_shells
    _, sid = await native_session(harness, repo, lease)
    _, jid = await create_tracked_job(harness, repo, lease, sid)
    arguments = {"session_id": sid, "retry": [jid]}
    with pytest.raises(NativeRetryRejected):
        await invoke(
            harness, repo, lease, ModelToolCall("early", "job", arguments)
        )
    row = intent_rows(repo)["early"]
    assert (
        row["state"] == "stopped"
        and row["native_id"] is None
        and len(started) == 1
    )
    assert (
        await harness.call(
            harness.executor_id, "job", {"cancel": [jid]}, session_id=sid
        )
    ).ok
    result = await invoke(
        harness, repo, lease, ModelToolCall("later", "job", arguments)
    )
    assert result["retried"][0]["attempts"] == 2 and len(started) == 2


async def copy_sessions(harness, repo, lease):
    task_id, source = await native_session(harness, repo, lease)
    destination = await invoke(
        harness,
        repo,
        lease,
        ModelToolCall(
            "copy-destination",
            "session_start",
            {
                "task_id": task_id,
                "executor_id": harness.executor_id,
                "workdir": "project",
            },
        ),
    )
    return source, destination["session_id"]


@pytest.mark.asyncio
async def test_managed_copy_lost_response_recovers_actual_file_and_refuses_duplicate(
    bound, tmp_path, monkeypatch
):
    harness, repo, lease, generation = bound
    runtime = harness.control
    source, destination = await copy_sessions(harness, repo, lease)
    _, actor = await next_generation(repo, lease, generation, "copy file")
    data = b"native reserved copy" * 1024
    (tmp_path / "payload.bin").write_bytes(data)
    await runtime.managed_jobs_runtime.start()
    original = runtime.session_copy_service.start_background
    offered = []

    async def lost(**kwargs):
        result = await original(**kwargs)
        offered.append(result.job_id)
        raise TimeoutError("copy response lost")

    monkeypatch.setattr(runtime.session_copy_service, "start_background", lost)
    args = {
        "src_session_id": source,
        "src_path": "payload.bin",
        "dst_session_id": destination,
        "dst_path": "result.bin",
    }
    call = ModelToolCall("copy", "session_copy", args)
    try:
        with pytest.raises(TimeoutError):
            await invoke(
                harness, repo, actor, call, scopes={"shell:read", "shell:write"}
            )
        row = intent_rows(repo)["copy"]
        assert row["state"] == "unknown" and row["native_id"] is None
        result = {}
        for _ in range(100):
            result = await invoke(
                harness, repo, actor, call, scopes={"shell:read", "shell:write"}
            )
            if result["status"] in {"succeeded", "failed"}:
                break
            await asyncio.sleep(0.02)
        assert result["status"] == "succeeded", result
        assert (
            result["recovered"] is True
            and result["job_id"] == row["idempotency_key"]
        )
        assert (tmp_path / "project" / "result.bin").read_bytes() == data
        assert offered == [result["job_id"]]
        with control_execution_context(
            config=runtime.config,
            state_store=runtime.state_store,
            oauth_state=runtime.oauth_state,
            managed_jobs_runtime=runtime.managed_jobs_runtime,
        ):
            with pytest.raises((RuntimeError, ValueError)):
                await original(**args, reserved_job_id=result["job_id"])
            receipt = await runtime.job_service.execute(
                session_id=source, poll=[result["job_id"]]
            )
        assert receipt.outputs[0].job.status == "succeeded"
        assert receipt.outputs[0].job.attempts == 1
        assert (tmp_path / "project" / "result.bin").read_bytes() == data
    finally:
        await runtime.managed_jobs_runtime.aclose()


@pytest.mark.asyncio
async def test_managed_copy_cancels_on_revocation_without_ending_reused_sessions(
    bound, monkeypatch
):
    from workgate.control.session_copy import SESSION_COPY_MANAGED_KIND

    harness, repo, lease, generation = bound
    runtime = harness.control
    source, destination = await copy_sessions(harness, repo, lease)
    _, actor = await next_generation(repo, lease, generation, "copy file")
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def wait(_context, _payload):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setitem(
        runtime.managed_jobs_runtime.handlers, SESSION_COPY_MANAGED_KIND, wait
    )
    await runtime.managed_jobs_runtime.start()
    call = ModelToolCall(
        "copy",
        "session_copy",
        {
            "src_session_id": source,
            "src_path": "payload.bin",
            "dst_session_id": destination,
            "dst_path": "result.bin",
        },
    )
    try:
        result = await invoke(
            harness, repo, actor, call, scopes={"shell:read", "shell:write"}
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        job_id = result["job_id"]
        native_lease = runtime.managed_jobs_runtime.leases[job_id]
        with (
            control_execution_context(
                config=runtime.config,
                state_store=runtime.state_store,
                oauth_state=runtime.oauth_state,
                managed_jobs_runtime=runtime.managed_jobs_runtime,
            ),
            pytest.raises(RuntimeError),
        ):
            await runtime.session_copy_service.start_background(
                src_session_id=source,
                src_path="payload.bin",
                dst_session_id=destination,
                dst_path="other.bin",
                reserved_job_id=job_id,
            )
        assert runtime.managed_jobs_runtime.leases[job_id] is native_lease
        assert not cancelled.is_set()
        WorkspaceRepository(repo.database).save_setting(
            actor.owner,
            "agent.machine_policy.conversation." + generation.conversation_id,
            {harness.executor_id: "read"},
        )
        assert await sweep_native_cancellations(runtime, repo) == 1
        assert cancelled.is_set()
        assert intent_rows(repo)["copy"]["state"] == "stopped"
        assert harness.executor.sessions.lookup(source) is not None
        assert harness.executor.sessions.lookup(destination) is not None
        assert result["job_id"] == intent_rows(repo)["copy"]["native_id"]
    finally:
        await runtime.managed_jobs_runtime.aclose()


@pytest.mark.asyncio
async def test_managed_copy_requires_both_owned_sessions_and_background_mode(
    bound,
):
    harness, repo, lease, _ = bound
    source, destination = await copy_sessions(harness, repo, lease)
    args = {
        "src_session_id": source,
        "src_path": "payload.bin",
        "dst_session_id": destination,
        "dst_path": "result.bin",
    }
    foreign = await harness.control.session_coordinator.start_session(
        executor_id=harness.executor_id
    )
    with pytest.raises(PermissionError, match="does not belong"):
        await invoke(
            harness,
            repo,
            lease,
            ModelToolCall(
                "foreign",
                "session_copy",
                args | {"dst_session_id": foreign["session_id"]},
            ),
        )
    for extra in (
        {"background": False},
        {"background": 1},
        {"reserved_job_id": "injected"},
        {"overwrite": "true"},
    ):
        with pytest.raises(ValueError):
            await invoke(
                harness,
                repo,
                lease,
                ModelToolCall("invalid", "session_copy", args | extra),
            )
    assert "foreign" not in intent_rows(repo) and "invalid" not in intent_rows(
        repo
    )


@pytest.mark.asyncio
async def test_copy_cleanup_does_not_cancel_newer_native_retry(
    bound, monkeypatch
):
    from workgate.control.session_copy import SESSION_COPY_MANAGED_KIND

    harness, repo, lease, generation = bound
    runtime = harness.control
    source, destination = await copy_sessions(harness, repo, lease)
    run, actor = await next_generation(repo, lease, generation, "copy file")
    launches = []
    stopped = []

    async def wait(_context, _payload):
        index = len(launches) + 1
        launches.append(index)
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(index)

    monkeypatch.setitem(
        runtime.managed_jobs_runtime.handlers, SESSION_COPY_MANAGED_KIND, wait
    )
    await runtime.managed_jobs_runtime.start()
    call = ModelToolCall(
        "copy",
        "session_copy",
        {
            "src_session_id": source,
            "src_path": "payload.bin",
            "dst_session_id": destination,
            "dst_path": "result.bin",
        },
    )
    try:
        result = await invoke(
            harness, repo, actor, call, scopes={"shell:read", "shell:write"}
        )
        jid = result["job_id"]
        assert repo.owned(
            actor.owner,
            generation.conversation_id,
            "job",
            jid,
            machine=harness.executor_id,
            session_id=source,
        )
        with control_execution_context(
            config=runtime.config,
            state_store=runtime.state_store,
            oauth_state=runtime.oauth_state,
            managed_jobs_runtime=runtime.managed_jobs_runtime,
        ):
            for _ in range(10):
                if launches:
                    break
                await asyncio.sleep(0)
            assert launches == [1]
            original = await runtime.job_service.execute(
                session_id=source, cancel=[jid]
            )
            assert original.cancelled[0].job.status == "stopped"
            newer = await runtime.job_service.execute(
                session_id=source, retry=[jid]
            )
            assert newer.retried[0].attempts == 2
            for _ in range(10):
                if len(launches) == 2:
                    break
                await asyncio.sleep(0)
        repo.cancel_run(actor.owner, run.run_id)
        assert await sweep_native_cancellations(runtime, repo) == 1
        assert intent_rows(repo)["copy"]["state"] == "stopped"
        assert launches == [1, 2] and stopped == [1]
        with control_execution_context(
            config=runtime.config,
            state_store=runtime.state_store,
            oauth_state=runtime.oauth_state,
            managed_jobs_runtime=runtime.managed_jobs_runtime,
        ):
            current = await runtime.job_service.execute(
                session_id=source, poll=[jid]
            )
            assert current.outputs[0].job.status == "running"
            await runtime.job_service.execute(session_id=source, cancel=[jid])
        assert stopped == [1, 2]
    finally:
        await runtime.managed_jobs_runtime.aclose()


@pytest.mark.asyncio
async def test_managed_retry_rejects_active_attempt_then_recovers_lost_response(
    bound, monkeypatch
):
    from workgate.control.session_copy import SESSION_COPY_MANAGED_KIND

    harness, repo, lease, generation = bound
    runtime = harness.control
    source, destination = await copy_sessions(harness, repo, lease)
    creation_run, creator = await next_generation(
        repo, lease, generation, "copy"
    )
    scopes = {"shell:read", "shell:write"}
    launches, stops = [], []
    entered = asyncio.Event()

    async def wait(_context, _payload):
        attempt = len(launches) + 1
        launches.append(attempt)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stops.append(attempt)

    monkeypatch.setitem(
        runtime.managed_jobs_runtime.handlers, SESSION_COPY_MANAGED_KIND, wait
    )
    await runtime.managed_jobs_runtime.start()
    try:
        created = await invoke(
            harness,
            repo,
            creator,
            ModelToolCall(
                "copy",
                "session_copy",
                {
                    "src_session_id": source,
                    "src_path": "a",
                    "dst_session_id": destination,
                    "dst_path": "b",
                },
            ),
            scopes=scopes,
        )
        jid = created["job_id"]
        await asyncio.wait_for(entered.wait(), timeout=2)
        args = {"session_id": source, "retry": [jid]}
        native_lease = runtime.managed_jobs_runtime.leases[jid]
        with pytest.raises(NativeRetryRejected):
            await invoke(
                harness,
                repo,
                creator,
                ModelToolCall("active", "job", args),
                scopes=scopes,
            )
        assert intent_rows(repo)["active"]["native_id"] is None
        assert intent_rows(repo)["active"]["state"] == "stopped"
        with control_execution_context(
            config=runtime.config,
            state_store=runtime.state_store,
            oauth_state=runtime.oauth_state,
            managed_jobs_runtime=runtime.managed_jobs_runtime,
        ):
            with pytest.raises(RuntimeError, match="still active"):
                await runtime.job_service.execute(**args)
            assert runtime.managed_jobs_runtime.leases[jid] is native_lease
            assert launches == [1] and stops == []
            await runtime.job_service.execute(session_id=source, cancel=[jid])
        assert stops == [1]
        run, actor = await next_generation(repo, creator, creation_run, "retry")
        original = runtime.session_copy_service.retry_reserved
        offered = []

        async def lost(*args, **kwargs):
            result = await original(*args, **kwargs)
            offered.append(result.attempts)
            raise TimeoutError("response lost")

        monkeypatch.setattr(
            runtime.session_copy_service, "retry_reserved", lost
        )
        call = ModelToolCall("retry-copy", "job", args)
        entered.clear()
        with pytest.raises(TimeoutError):
            await invoke(harness, repo, actor, call, scopes=scopes)
        row = intent_rows(repo)["retry-copy"]
        assert (
            row["state"] == "unknown"
            and row["destination_session_id"] == destination
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        recovered = await invoke(harness, repo, actor, call, scopes=scopes)
        assert recovered["recovered"] is True
        assert recovered["retried"][0]["attempts"] == 2 and offered == [2]
        assert launches == [1, 2] and stops == [1]
        repo.cancel_run(creator.owner, creator.run_id)
        assert await sweep_native_cancellations(runtime, repo) >= 1
        assert stops == [1]
        assert intent_rows(repo)["copy"]["state"] == "stopped"
        repo.cancel_run(actor.owner, run.run_id)
        assert await sweep_native_cancellations(runtime, repo) == 1
        assert stops == [1, 2]
        assert intent_rows(repo)["retry-copy"]["state"] == "stopped"
        assert harness.executor.sessions.lookup(source) is not None
        assert harness.executor.sessions.lookup(destination) is not None
    finally:
        await runtime.managed_jobs_runtime.aclose()


def batch_intent_rows(repo, call_id):
    with repo.database.read_snapshot() as db:
        return {
            row["target_job_id"]: dict(row)
            for row in db.execute(
                "SELECT * FROM agent_durable_tasks WHERE call_id=?", (call_id,)
            )
        }


@pytest.mark.asyncio
async def test_batch_retry_preflights_all_targets_and_recovers_in_order(
    bound, tracked_shells, monkeypatch
):
    from workgate.protocol.ids import new_reserved_job_id

    harness, repo, lease, generation = bound
    started, killed = tracked_shells
    _, sid = await native_session(harness, repo, lease)
    ids = []
    for index in range(2):
        result = await invoke(
            harness,
            repo,
            lease,
            ModelToolCall(
                f"job-{index}",
                "bash",
                {
                    "session_id": sid,
                    "command": f"tracked-{index}",
                    "async_": True,
                },
            ),
        )
        ids.append(result["result"]["job_id"])
    assert (
        await harness.call(
            harness.executor_id, "job", {"cancel": ids}, session_id=sid
        )
    ).ok
    run, actor = await next_generation(repo, lease, generation, "retry batch")
    transport = harness.control.executor_transport
    original = transport.call
    offered = []

    async def lost(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        result = await original(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )
        if op == JOB_RETRY_RESERVED_OP:
            assert args is not None and result.ok
            rows = batch_intent_rows(repo, "batch")
            assert set(rows) == set(ids)
            offered.append(args["job_id"])
            if args["job_id"] == ids[0]:
                raise TimeoutError("first response lost")
        return result

    monkeypatch.setattr(transport, "call", lost)
    call = ModelToolCall("batch", "job", {"session_id": sid, "retry": ids})
    with pytest.raises(PermissionError, match="does not belong"):
        await invoke(
            harness,
            repo,
            actor,
            ModelToolCall(
                "foreign-batch",
                "job",
                {"session_id": sid, "retry": [ids[0], new_reserved_job_id()]},
            ),
        )
    assert batch_intent_rows(repo, "foreign-batch") == {}
    assert offered == [] and len(started) == 2
    with pytest.raises(TimeoutError):
        await invoke(harness, repo, actor, call)
    rows = batch_intent_rows(repo, "batch")
    assert rows[ids[0]]["state"] == "unknown"
    assert rows[ids[1]]["state"] == "active"
    assert len({row["idempotency_key"] for row in rows.values()}) == 2
    assert all(row["native_attempt"] == 2 for row in rows.values())
    assert offered == ids and len(started) == 4
    for changed in (
        ModelToolCall("batch", "job", {"session_id": sid, "retry": ids[::-1]}),
        ModelToolCall("batch", "job", {"session_id": sid, "retry": ids[:1]}),
        ModelToolCall("batch", "task", {"action": "create"}),
    ):
        with pytest.raises(PermissionError, match="cannot change"):
            await invoke(harness, repo, actor, changed)
    recovered = await invoke(harness, repo, actor, call)
    assert recovered["recovered"] is True
    assert [job["job_id"] for job in recovered["retried"]] == ids
    assert [job["attempts"] for job in recovered["retried"]] == [2, 2]
    assert offered == ids and len(started) == 4
    repo.cancel_run(actor.owner, run.run_id)
    assert await sweep_native_cancellations(harness.control, repo) == 2
    assert all(
        row["state"] == "stopped"
        for row in batch_intent_rows(repo, "batch").values()
    )
    assert killed == started


@pytest.mark.asyncio
async def test_batch_retry_replays_definitive_refusal_without_reoffering_siblings(
    bound, tracked_shells, monkeypatch
):
    harness, repo, lease, _ = bound
    started, _ = tracked_shells
    _, sid = await native_session(harness, repo, lease)
    ids = []
    for index in range(2):
        result = await invoke(
            harness,
            repo,
            lease,
            ModelToolCall(
                f"job-{index}",
                "bash",
                {"session_id": sid, "command": "tracked", "async_": True},
            ),
        )
        ids.append(result["result"]["job_id"])
    # The first is still active; the second can be retried.
    assert (
        await harness.call(
            harness.executor_id, "job", {"cancel": ids[1:]}, session_id=sid
        )
    ).ok
    original = harness.control.executor_transport.call
    offered = []

    async def trace(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        if op == JOB_RETRY_RESERVED_OP:
            assert args is not None
            offered.append(args["job_id"])
        return await original(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )

    monkeypatch.setattr(harness.control.executor_transport, "call", trace)
    call = ModelToolCall(
        "batch-refusal", "job", {"session_id": sid, "retry": ids}
    )
    for _ in range(2):
        with pytest.raises(NativeRetryRejected):
            await invoke(harness, repo, lease, call)
    rows = batch_intent_rows(repo, "batch-refusal")
    assert (
        rows[ids[0]]["state"] == "stopped" and rows[ids[0]]["native_id"] is None
    )
    assert rows[ids[1]]["state"] == "active"
    assert offered == ids and len(started) == 3


@pytest.mark.asyncio
async def test_mixed_retry_batch_preflights_each_native_grant(
    bound, tracked_shells, monkeypatch
):
    from workgate.control.session_copy import SESSION_COPY_MANAGED_KIND

    harness, repo, lease, generation = bound
    started, _ = tracked_shells
    runtime = harness.control
    source, destination = await copy_sessions(harness, repo, lease)
    entered = asyncio.Event()
    copy_attempts = []

    async def wait(_context, _payload):
        copy_attempts.append(1)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setitem(
        runtime.managed_jobs_runtime.handlers, SESSION_COPY_MANAGED_KIND, wait
    )
    await runtime.managed_jobs_runtime.start()
    try:
        _, shell = await create_tracked_job(harness, repo, lease, source)
        copy_result = await invoke(
            harness,
            repo,
            lease,
            ModelToolCall(
                "copy",
                "session_copy",
                {
                    "src_session_id": source,
                    "src_path": "a",
                    "dst_session_id": destination,
                    "dst_path": "b",
                },
            ),
        )
        managed = copy_result["job_id"]
        await asyncio.wait_for(entered.wait(), timeout=2)
        with control_execution_context(
            config=runtime.config,
            state_store=runtime.state_store,
            oauth_state=runtime.oauth_state,
            managed_jobs_runtime=runtime.managed_jobs_runtime,
        ):
            await runtime.job_service.execute(
                session_id=source, cancel=[managed, shell]
            )
        _, actor = await next_generation(repo, lease, generation, "mixed retry")
        call = ModelToolCall(
            "mixed", "job", {"session_id": source, "retry": [managed, shell]}
        )
        with pytest.raises(PermissionError, match="shell:execute"):
            await invoke(
                harness, repo, actor, call, scopes={"shell:read", "shell:write"}
            )
        assert batch_intent_rows(repo, "mixed") == {}
        assert len(copy_attempts) == len(started) == 1
        entered.clear()
        result = await invoke(harness, repo, actor, call)
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert [job["job_id"] for job in result["retried"]] == [managed, shell]
        assert [job["kind"] for job in result["retried"]] == [
            "managed",
            "shell",
        ]
        assert [job["attempts"] for job in result["retried"]] == [2, 2]
        rows = batch_intent_rows(repo, "mixed")
        assert rows[managed]["destination_session_id"] == destination
        assert rows[shell]["destination_session_id"] is None
        assert len(copy_attempts) == len(started) == 2
    finally:
        await runtime.managed_jobs_runtime.aclose()


async def poll_native(harness, repo, lease, args, *, scopes=None):
    if scopes is None:
        scopes = {"shell:read"}
    return await inspect_native_jobs(
        harness.control,
        repo,
        lease,
        ModelToolCall("poll", "job", args),
        machine=harness.executor_id,
        scopes=scopes,
        expires=None,
    )


@pytest.mark.asyncio
async def test_poll_only_exact_owned_attempt_with_read_scope(
    bound, tracked_shells, monkeypatch
):
    from workgate.protocol.ids import new_reserved_job_id

    harness, repo, lease, generation = bound
    _, sid = await native_session(harness, repo, lease)
    _, jid = await create_tracked_job(harness, repo, lease, sid)
    original = harness.control.executor_transport.call
    lookups = []

    async def trace(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        if op == JOB_LOOKUP_ATTEMPT_OP:
            lookups.append(dict(args or {}))
        return await original(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )

    monkeypatch.setattr(harness.control.executor_transport, "call", trace)
    args = {"session_id": sid, "poll": [jid], "lines": 7}
    with pytest.raises(PermissionError, match="does not belong"):
        await poll_native(
            harness, repo, lease, args | {"poll": [jid, new_reserved_job_id()]}
        )
    with pytest.raises(PermissionError, match="shell:read"):
        await poll_native(harness, repo, lease, args, scopes=set())
    for changed in (
        {"lines": 0},
        {"lines": 5001},
        {"poll": [jid, jid]},
        {"cancel": [jid]},
    ):
        with pytest.raises(ValueError):
            await poll_native(harness, repo, lease, args | changed)
    assert lookups == []
    result = await poll_native(harness, repo, lease, args)
    assert result["operation"] == "poll"
    assert result["outputs"][0]["job"]["job_id"] == jid
    assert result["outputs"][0]["output"] == "native output"
    assert lookups == [
        {"job_id": jid, "attempt": 1, "retry_key": None, "lines": 7}
    ]
    assert (
        await harness.call(
            harness.executor_id, "job", {"cancel": [jid]}, session_id=sid
        )
    ).ok
    assert (
        await harness.call(
            harness.executor_id, "job", {"retry": [jid]}, session_id=sid
        )
    ).ok
    with pytest.raises(NativeCreationUncertain, match="superseded"):
        await poll_native(harness, repo, lease, args)
    assert len(lookups) == 2


@pytest.mark.asyncio
async def test_managed_poll_uses_read_grants_and_exact_native_attempt(
    bound, monkeypatch
):
    from workgate.control.session_copy import SESSION_COPY_MANAGED_KIND

    harness, repo, lease, generation = bound
    runtime = harness.control
    source, destination = await copy_sessions(harness, repo, lease)
    entered = asyncio.Event()

    async def wait(context, _payload):
        await context.log("owned managed attempt")
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setitem(
        runtime.managed_jobs_runtime.handlers, SESSION_COPY_MANAGED_KIND, wait
    )
    await runtime.managed_jobs_runtime.start()
    try:
        result = await invoke(
            harness,
            repo,
            lease,
            ModelToolCall(
                "copy",
                "session_copy",
                {
                    "src_session_id": source,
                    "src_path": "a",
                    "dst_session_id": destination,
                    "dst_path": "b",
                },
            ),
        )
        jid = result["job_id"]
        await asyncio.wait_for(entered.wait(), timeout=2)
        policy = (
            "agent.machine_policy.conversation." + generation.conversation_id
        )
        workspace = WorkspaceRepository(repo.database)
        workspace.save_setting(
            lease.owner, policy, {harness.executor_id: "read"}
        )
        args = {"session_id": source, "poll": [jid], "lines": 1}
        poll = await poll_native(harness, repo, lease, args)
        assert poll["outputs"][0]["job"]["kind"] == "managed"
        assert "owned managed attempt" in poll["outputs"][0]["output"]
        listing = await poll_native(
            harness, repo, lease, {"session_id": source}
        )
        assert [job["job_id"] for job in listing["jobs"]] == [jid]
        assert listing["jobs"][0]["kind"] == "managed"
        assert listing["jobs"][0]["progress"] is None
        assert listing["jobs"][0]["result"] is None
        workspace.save_setting(
            lease.owner, policy, {harness.executor_id: "none"}
        )
        with pytest.raises(PermissionError):
            await poll_native(harness, repo, lease, args)
        workspace.save_setting(
            lease.owner, policy, {harness.executor_id: "write"}
        )
        with control_execution_context(
            config=runtime.config,
            state_store=runtime.state_store,
            oauth_state=runtime.oauth_state,
            managed_jobs_runtime=runtime.managed_jobs_runtime,
        ):
            await runtime.job_service.execute(session_id=source, cancel=[jid])
            await runtime.job_service.execute(session_id=source, retry=[jid])
        with pytest.raises(NativeCreationUncertain, match="superseded"):
            await poll_native(harness, repo, lease, args)
        listing = await poll_native(
            harness, repo, lease, {"session_id": source}
        )
        assert listing["total"] == 0
    finally:
        await runtime.managed_jobs_runtime.aclose()


@pytest.mark.asyncio
async def test_poll_rechecks_revocation_after_native_reply(
    bound, tracked_shells, monkeypatch
):
    harness, repo, lease, generation = bound
    _, sid = await native_session(harness, repo, lease)
    _, jid = await create_tracked_job(harness, repo, lease, sid)
    transport = harness.control.executor_transport
    original = transport.call

    async def revoked(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        result = await original(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )
        if op == JOB_LOOKUP_ATTEMPT_OP:
            WorkspaceRepository(repo.database).save_setting(
                lease.owner,
                "agent.machine_policy.conversation."
                + generation.conversation_id,
                {harness.executor_id: "none"},
            )
        return result

    monkeypatch.setattr(transport, "call", revoked)
    with pytest.raises(PermissionError):
        await poll_native(
            harness, repo, lease, {"session_id": sid, "poll": [jid]}
        )


@pytest.mark.asyncio
async def test_owned_job_list_filters_manual_and_superseded_attempts(
    bound, tracked_shells
):
    harness, repo, lease, _ = bound
    _, sid = await native_session(harness, repo, lease)
    ids = []
    for i in range(3):
        job = await invoke(
            harness,
            repo,
            lease,
            ModelToolCall(
                f"owned-{i}",
                "bash",
                {
                    "session_id": sid,
                    "command": f"owned-{i}",
                    "async_": True,
                },
            ),
        )
        ids.append(job["result"]["job_id"])
    manual = await harness.call(
        harness.executor_id,
        "bash",
        {"command": "manual secret", "async_": True},
        session_id=sid,
    )
    assert manual.ok
    for job_id in (ids[0], ids[1]):
        assert (
            await harness.call(
                harness.executor_id, "job", {"cancel": [job_id]}, session_id=sid
            )
        ).ok
    assert (
        await harness.call(
            harness.executor_id, "job", {"retry": [ids[0]]}, session_id=sid
        )
    ).ok

    args = {"session_id": sid, "limit": 1}
    first = await poll_native(harness, repo, lease, args)
    assert first["operation"] == "list"
    assert first["total"] == 2
    assert sum(first["counts"].values()) == 2
    assert first["next_offset"] == 1
    second = await poll_native(harness, repo, lease, args | {"offset": 1})
    assert {first["jobs"][0]["job_id"], second["jobs"][0]["job_id"]} == set(
        ids[1:]
    )
    assert second["next_offset"] is None
    running = await poll_native(
        harness, repo, lease, args | {"include_finished": False}
    )
    assert running["total"] == 1
    assert running["jobs"][0]["job_id"] == ids[2]
    assert sum(running["counts"].values()) == 2
    with pytest.raises(PermissionError, match="shell:read"):
        await poll_native(harness, repo, lease, args, scopes=set())


@pytest.mark.asyncio
async def test_empty_job_list_requires_owned_session(bound):
    harness, repo, lease, _ = bound
    _, sid = await native_session(harness, repo, lease)
    result = await poll_native(harness, repo, lease, {"session_id": sid})
    assert result["total"] == 0
    assert result["jobs"] == []
    assert result["counts"] == {}
    from workgate.protocol.ids import new_session_id

    with pytest.raises(PermissionError, match="does not belong"):
        await poll_native(
            harness, repo, lease, {"session_id": new_session_id()}
        )


async def cancel_native(harness, repo, lease, call, *, scopes=None):
    from workgate.control.hosted_chat_cancellation import cancel_native_jobs

    return await cancel_native_jobs(
        harness.control,
        repo,
        lease,
        call,
        machine=harness.executor_id,
        scopes=SCOPES if scopes is None else scopes,
        expires=None,
    )


@pytest.mark.asyncio
async def test_explicit_cancel_preflights_and_pins_replayed_attempt(
    bound,
    tracked_shells,
):
    from workgate.protocol.ids import new_reserved_job_id

    harness, repo, lease, generation = bound
    _, sid = await native_session(harness, repo, lease)
    _, jid = await create_tracked_job(harness, repo, lease, sid)
    args = {"session_id": sid, "cancel": [jid]}
    call = ModelToolCall("explicit-cancel", "job", args)
    with pytest.raises(PermissionError):
        await cancel_native(harness, repo, lease, call, scopes={"shell:read"})
    with pytest.raises(PermissionError, match="shell:execute"):
        await cancel_native(
            harness, repo, lease, call, scopes={"shell:read", "shell:write"}
        )
    with pytest.raises(PermissionError):
        await cancel_native(
            harness,
            repo,
            lease,
            ModelToolCall(
                "invalid-cancel",
                "job",
                args | {"cancel": [jid, new_reserved_job_id()]},
            ),
        )
    assert (
        await poll_native(
            harness, repo, lease, {"session_id": sid, "poll": [jid]}
        )
    )["outputs"][0]["job"]["status"] == "running"
    result = await cancel_native(harness, repo, lease, call)
    assert result["cancelled"][0]["job"]["status"] == "stopped"
    assert result["cancelled"][0]["killed"] is True
    repeat = await cancel_native(harness, repo, lease, call)
    assert repeat["cancelled"][0]["killed"] is False
    assert (
        await harness.call(
            harness.executor_id, "job", {"retry": [jid]}, session_id=sid
        )
    ).ok
    with pytest.raises(NativeCreationUncertain, match="superseded"):
        await cancel_native(harness, repo, lease, call)
    native = await harness.call(
        harness.executor_id, "job", {"poll": [jid]}, session_id=sid
    )
    assert native.ok
    assert native.result["outputs"][0]["job"]["status"] == "running"


@pytest.mark.asyncio
async def test_unified_owned_job_dispatch_uses_exact_operation_adapters(
    bound, tracked_shells
):
    from workgate.control.hosted_chat_job_dispatch import dispatch_native_job

    harness, repo, lease, _ = bound
    _, sid = await native_session(harness, repo, lease)
    _, jid = await create_tracked_job(harness, repo, lease, sid)

    async def dispatch(call_id, args, scopes=SCOPES):
        return await dispatch_native_job(
            harness.control,
            repo,
            lease,
            ModelToolCall(call_id, "job", {"session_id": sid} | args),
            machine=harness.executor_id,
            scopes=scopes,
            expires=None,
        )

    assert (await dispatch("list", {"list_jobs": True}, {"shell:read"}))[
        "total"
    ] == 1
    for args in (
        {"poll": [jid], "cancel": [jid]},
        {"list_jobs": True, "retry": [jid]},
        {"cancel": [jid, jid]},
        {"poll": []},
    ):
        with pytest.raises(ValueError):
            await dispatch("invalid", args)
    poll = await dispatch(
        "read", {"poll": [jid], "cancel": None, "retry": None}, {"shell:read"}
    )
    assert poll["outputs"][0]["job"]["status"] == "running"
    result = await dispatch("stop", {"cancel": [jid], "list_jobs": False})
    assert result["cancelled"][0]["job"]["status"] == "stopped"
    with pytest.raises(PermissionError, match="cannot change"):
        await dispatch("stop", {"retry": [jid]})
    retry = await dispatch("retry", {"retry": [jid], "poll": None})
    assert retry["retried"][0]["attempts"] == 2
    with pytest.raises(PermissionError, match="cannot change"):
        await dispatch("retry", {"cancel": [jid]})
    poll = await dispatch("read-new", {"poll": [jid]}, {"shell:read"})
    assert poll["outputs"][0]["job"]["attempts"] == 2


@pytest.mark.asyncio
async def test_owned_file_reads_use_native_catalog_and_live_acl(
    bound, tmp_path
):
    from workgate.control.hosted_chat_session_tools import (
        NativeSessionDispatcher,
    )
    from workgate.protocol.ids import new_session_id

    harness, repo, lease, generation = bound
    runtime = harness.control
    _, sid = await native_session(harness, repo, lease)
    (tmp_path / "owned.txt").write_text("native file payload\n")
    dispatcher = NativeSessionDispatcher(runtime)

    async def dispatch(name, args, scopes=None):
        return await dispatcher.dispatch(
            repo,
            lease,
            ModelToolCall("read-call", name, args),
            machine=harness.executor_id,
            scopes={"shell:read"} if scopes is None else scopes,
            expires=None,
        )

    args = {"session_id": sid, "path": str(tmp_path / "owned.txt")}
    workspace = WorkspaceRepository(repo.database)
    policy = "agent.machine_policy.conversation." + generation.conversation_id
    workspace.save_setting(lease.owner, policy, {harness.executor_id: "read"})
    result = await dispatch("read", args)
    assert not result.get("isError")
    assert "native file payload" in str(result["content"])
    assert "structuredContent" in result
    listing = await dispatch(
        "list_files", {"session_id": sid, "path": str(tmp_path)}
    )
    assert "owned.txt" in str(listing["structuredContent"])
    for name, values in (
        ("tree_view", {"cwd": str(tmp_path)}),
        ("glob_search", {"cwd": str(tmp_path), "pattern": "*.txt"}),
        (
            "search",
            {
                "paths": [str(tmp_path / "owned.txt")],
                "pattern": "native file payload",
            },
        ),
    ):
        result = await dispatch(name, {"session_id": sid} | values)
        assert not result.get("isError")
        if name == "search" and shutil.which("rg") is None:
            # Native search is optional on this platform. The canonical tool
            # reports the missing executable rather than inventing matches.
            assert result["structuredContent"]["ok"] is False
            assert result["structuredContent"]["matches"] == []
            assert "rg:" in result["structuredContent"]["stderr"]
        else:
            assert "owned.txt" in str(result["structuredContent"])

    with pytest.raises(PermissionError, match="does not belong"):
        await dispatch("read", args | {"session_id": new_session_id()})
    with pytest.raises(PermissionError, match="shell:read"):
        await dispatch("read", args, set())
    with pytest.raises(PermissionError, match="read-only"):
        await dispatch("write_file", args | {"content": "unapproved"})
    assert (tmp_path / "owned.txt").read_text() == "native file payload\n"
    rows = [
        json.loads(line)
        for line in get_settings()
        .audit_log_path.read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    assert any(
        row.get("tool") == "read"
        and row.get("transport") == "hosted-chat"
        and row.get("event") == "tool_call_end"
        and row.get("ok") is True
        for row in rows
    )
    workspace.save_setting(lease.owner, policy, {harness.executor_id: "none"})
    with pytest.raises(PermissionError):
        await dispatch("read", args)


@pytest.mark.asyncio
async def test_owned_file_read_discards_result_on_revocation(
    bound, tmp_path, monkeypatch
):
    from workgate.control.hosted_chat_session_tools import (
        NativeSessionDispatcher,
    )

    harness, repo, lease, generation = bound
    runtime = harness.control
    _, sid = await native_session(harness, repo, lease)
    (tmp_path / "owned.txt").write_text("native file payload")
    transport = runtime.executor_transport
    original = transport.call

    async def revoked(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        result = await original(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )
        if op == "read":
            WorkspaceRepository(repo.database).save_setting(
                lease.owner,
                "agent.machine_policy.conversation."
                + generation.conversation_id,
                {harness.executor_id: "none"},
            )
        return result

    monkeypatch.setattr(transport, "call", revoked)
    dispatcher = NativeSessionDispatcher(runtime)
    with pytest.raises(PermissionError):
        await dispatcher.dispatch(
            repo,
            lease,
            ModelToolCall(
                "read-call",
                "read",
                {"session_id": sid, "path": str(tmp_path / "owned.txt")},
            ),
            machine=harness.executor_id,
            scopes={"shell:read"},
            expires=None,
        )


@pytest.mark.asyncio
async def test_owned_file_read_rechecks_while_native_call_is_pending(
    bound, tmp_path, monkeypatch
):
    from workgate.control.hosted_chat_session_tools import (
        NativeSessionDispatcher,
    )

    harness, repo, lease, generation = bound
    runtime = harness.control
    _, sid = await native_session(harness, repo, lease)
    entered = asyncio.Event()
    released = asyncio.Event()
    transport = runtime.executor_transport
    original = transport.call

    async def delayed(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        if op != "read":
            return await original(
                executor_id,
                op,
                args,
                session_id=session_id,
                timeout_s=timeout_s,
            )
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    monkeypatch.setattr(transport, "call", delayed)
    dispatcher = NativeSessionDispatcher(runtime)
    task = asyncio.create_task(
        dispatcher.dispatch(
            repo,
            lease,
            ModelToolCall(
                "pending-read",
                "read",
                {
                    "session_id": sid,
                    "path": str(tmp_path / "owned.txt"),
                },
            ),
            machine=harness.executor_id,
            scopes={"shell:read"},
            expires=None,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        WorkspaceRepository(repo.database).save_setting(
            lease.owner,
            "agent.machine_policy.conversation." + generation.conversation_id,
            {harness.executor_id: "none"},
        )
        with pytest.raises(PermissionError):
            await asyncio.wait_for(task, timeout=5)
        assert released.is_set()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_owned_shell_read_and_list_hide_unowned_native_handles(
    bound,
    reserved_shell_backend,
    monkeypatch,
):
    from workgate.control.hosted_chat_session_tools import (
        NativeSessionDispatcher,
    )
    from workgate.protocol.ids import new_reserved_shell_id
    from workgate.schemas.result_models.shell import (
        ListPersistentShellsOutput,
        PersistentShellInfo,
    )

    harness, repo, lease, generation = bound
    started, _ = reserved_shell_backend
    _, sid = await native_session(harness, repo, lease)
    result = await invoke(
        harness,
        repo,
        lease,
        ModelToolCall(
            "pty-inspect",
            "bash",
            {"session_id": sid, "pty": True, "command": "shell"},
        ),
    )
    owned = result["result"]["shell_id"]
    foreign = new_reserved_shell_id()
    started[foreign] = sid

    async def inventory(_config, _store):
        return ListPersistentShellsOutput(
            shells=[
                PersistentShellInfo(shell_id=owned, command="owned command"),
                PersistentShellInfo(shell_id=foreign, command="unowned marker"),
            ]
        )

    async def read(_config, shell_id, lines=200, *, preserve_ansi=False):
        return ReadPersistentShellOutput(
            shell_id=shell_id, output="owned output", lines=lines
        )

    monkeypatch.setattr(
        shell_service, "list_persistent_shells_execute", inventory
    )
    monkeypatch.setattr(
        shell_service, "read_persistent_shell_output_execute", read
    )
    dispatcher = NativeSessionDispatcher(harness.control)

    async def dispatch(name, args):
        return await dispatcher.dispatch(
            repo,
            lease,
            ModelToolCall("shell-inspect", name, {"session_id": sid} | args),
            machine=harness.executor_id,
            scopes={"shell:read"},
            expires=None,
        )

    listing = await dispatch("list_persistent_shells", {})
    assert [
        row["shell_id"] for row in listing["structuredContent"]["shells"]
    ] == [owned]
    assert foreign not in str(listing)
    assert "unowned marker" not in str(listing)
    read_result = await dispatch(
        "read_persistent_shell_output", {"shell_id": owned, "lines": 7}
    )
    assert read_result["structuredContent"]["output"] == "owned output"
    assert read_result["structuredContent"]["lines"] == 7

    async def mismatched_read(
        _config, shell_id, lines=200, *, preserve_ansi=False
    ):
        return ReadPersistentShellOutput(
            shell_id=foreign, output="unowned marker", lines=lines
        )

    monkeypatch.setattr(
        shell_service, "read_persistent_shell_output_execute", mismatched_read
    )
    with pytest.raises(RuntimeError, match="mismatched identity"):
        await dispatch("read_persistent_shell_output", {"shell_id": owned})
    with pytest.raises(PermissionError, match="Shell does not belong"):
        await dispatch("read_persistent_shell_output", {"shell_id": foreign})
    WorkspaceRepository(repo.database).save_setting(
        lease.owner,
        "agent.machine_policy.conversation." + generation.conversation_id,
        {harness.executor_id: "none"},
    )
    with pytest.raises(PermissionError):
        await dispatch("list_persistent_shells", {})


@pytest.mark.asyncio
async def test_owned_shell_cancel_is_durable_and_replay_does_not_kill_other_shells(
    bound, reserved_shell_backend, monkeypatch
):
    from workgate.control.hosted_chat_cancellation import cancel_native_shell
    from workgate.protocol.ids import new_reserved_shell_id

    harness, repo, lease, _ = bound
    started, killed = reserved_shell_backend
    _, sid = await native_session(harness, repo, lease)
    result = await invoke(
        harness,
        repo,
        lease,
        ModelToolCall(
            "pty-stop",
            "bash",
            {"session_id": sid, "pty": True, "command": "shell"},
        ),
    )
    owned = result["result"]["shell_id"]
    foreign = new_reserved_shell_id()
    started[foreign] = sid
    call = ModelToolCall(
        "explicit-shell-stop",
        "kill_persistent_shell",
        {"session_id": sid, "shell_id": owned},
    )

    async def dispatch(proposal, scopes=SCOPES):
        return await cancel_native_shell(
            harness.control,
            repo,
            lease,
            proposal,
            machine=harness.executor_id,
            scopes=scopes,
            expires=None,
        )

    with pytest.raises(PermissionError):
        await dispatch(call, {"shell:read"})
    with pytest.raises(PermissionError, match="Shell does not belong"):
        await dispatch(
            ModelToolCall(
                "foreign-stop",
                "kill_persistent_shell",
                {"session_id": sid, "shell_id": foreign},
            )
        )
    assert not killed
    native = harness.control.executor_transport.call

    async def lose_reply(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        response = await native(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )
        if op == "kill_persistent_shell":
            assert intent_rows(repo)["pty-stop"]["state"] == "cancel_pending"
            raise TimeoutError("lost stop response")
        return response

    monkeypatch.setattr(harness.control.executor_transport, "call", lose_reply)
    with pytest.raises(TimeoutError):
        await dispatch(call)
    assert killed == [owned]
    assert intent_rows(repo)["pty-stop"]["state"] == "cancel_pending"
    monkeypatch.setattr(harness.control.executor_transport, "call", native)
    result = await dispatch(call)
    assert result["shell_id"] == owned and result["killed"] is False
    assert intent_rows(repo)["pty-stop"]["state"] == "stopped"
    with pytest.raises(PermissionError, match="cannot change"):
        await dispatch(
            ModelToolCall(
                call.call_id,
                call.name,
                {"session_id": sid, "shell_id": foreign},
            )
        )
    assert killed == [owned] and started == {foreign: sid}


@pytest.mark.asyncio
async def test_owned_shell_effects_are_claimed_once_and_fenced_on_replay(
    bound,
    reserved_shell_backend,
    monkeypatch,
):
    from mcp.server.mcpserver.exceptions import ToolError

    from workgate.control.hosted_chat_session_tools import (
        NativeSessionDispatcher,
    )
    from workgate.protocol.ids import new_reserved_shell_id
    from workgate.schemas.result_models.shell import (
        ResizePersistentShellOutput,
        SendPersistentShellInputOutput,
    )

    harness, repo, lease, generation = bound
    started, _ = reserved_shell_backend
    _, sid = await native_session(harness, repo, lease)
    result = await invoke(
        harness,
        repo,
        lease,
        ModelToolCall(
            "shell-effect-start",
            "bash",
            {"session_id": sid, "pty": True, "command": "shell"},
        ),
    )
    owned = result["result"]["shell_id"]
    foreign = new_reserved_shell_id()
    started[foreign] = sid
    sent = []
    resized = []

    async def send(_config, shell_id, input_text, enter=True):
        sent.append((shell_id, input_text, enter))
        return SendPersistentShellInputOutput(
            shell_id=shell_id, sent_bytes=len(input_text.encode()), enter=enter
        )

    async def resize(_config, shell_id, cols, rows):
        resized.append((shell_id, cols, rows))
        return ResizePersistentShellOutput(
            shell_id=shell_id,
            cols=cols,
            rows=rows,
            resized=True,
            backend="fake",
        )

    monkeypatch.setattr(
        shell_service, "send_persistent_shell_input_execute", send
    )
    monkeypatch.setattr(
        shell_service, "resize_persistent_shell_execute", resize
    )
    dispatcher = NativeSessionDispatcher(harness.control)
    send_call = ModelToolCall(
        "send-effect",
        "send_persistent_shell_input",
        {
            "session_id": sid,
            "shell_id": owned,
            "input_text": "hello",
        },
    )
    resize_call = ModelToolCall(
        "resize-effect",
        "resize_persistent_shell",
        {
            "session_id": sid,
            "shell_id": owned,
            "cols": 100,
            "rows": 30,
        },
    )

    async def dispatch(call, scopes=SCOPES):
        return await dispatcher.dispatch(
            repo,
            lease,
            call,
            machine=harness.executor_id,
            scopes=scopes,
            expires=None,
        )

    with pytest.raises(PermissionError, match="shell:execute"):
        await dispatch(send_call, {"shell:read", "shell:write"})
    with pytest.raises(PermissionError, match="Shell does not belong"):
        await dispatch(
            ModelToolCall(
                "foreign-effect",
                send_call.name,
                send_call.arguments | {"shell_id": foreign},
            )
        )
    assert not sent
    result = await dispatch(send_call)
    assert result["structuredContent"]["sent_bytes"] == 5
    assert await dispatch(send_call) == result
    assert sent == [(owned, "hello", True)]
    with pytest.raises(PermissionError, match="cannot change"):
        await dispatch(
            ModelToolCall(
                send_call.call_id, resize_call.name, resize_call.arguments
            )
        )
    with pytest.raises(PermissionError, match="cannot change"):
        await invoke(
            harness,
            repo,
            lease,
            ModelToolCall(
                send_call.call_id,
                "bash",
                {
                    "session_id": sid,
                    "async_": True,
                    "command": "unused",
                },
            ),
        )
    result = await dispatch(resize_call)
    assert result["structuredContent"]["resized"] is True
    assert await dispatch(resize_call) == result
    assert resized == [(owned, 100, 30)]

    native = harness.control.executor_transport.call

    async def lose_reply(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        result = await native(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )
        if op == "send_persistent_shell_input":
            with repo.database.connect() as db:
                assert (
                    db.execute(
                        "SELECT receipt FROM agent_native_effect_calls WHERE call_id='lost-effect'"
                    ).fetchone()[0]
                    is None
                )
            raise TimeoutError("lost input response")
        return result

    monkeypatch.setattr(harness.control.executor_transport, "call", lose_reply)
    lost = ModelToolCall(
        "lost-effect",
        send_call.name,
        send_call.arguments | {"input_text": "once"},
    )
    with pytest.raises(ToolError, match="tool timeout"):
        await dispatch(lost)
    monkeypatch.setattr(harness.control.executor_transport, "call", native)
    with pytest.raises(NativeCreationUncertain, match="do not resend"):
        await dispatch(lost)
    assert sent == [(owned, "hello", True), (owned, "once", True)]
    from workgate.control.hosted_chat_cancellation import cancel_native_shell

    with pytest.raises(PermissionError, match="cannot change"):
        await cancel_native_shell(
            harness.control,
            repo,
            lease,
            ModelToolCall(
                lost.call_id,
                "kill_persistent_shell",
                {"session_id": sid, "shell_id": owned},
            ),
            machine=harness.executor_id,
            scopes=SCOPES,
            expires=None,
        )
    WorkspaceRepository(repo.database).save_setting(
        lease.owner,
        "agent.machine_policy.conversation." + generation.conversation_id,
        {harness.executor_id: "read"},
    )
    with pytest.raises(PermissionError, match="read-only"):
        await dispatch(send_call)
    assert started[owned] == sid and started[foreign] == sid


@pytest.mark.asyncio
async def test_owned_file_effects_preserve_native_grounding_and_replay(
    bound, tmp_path, monkeypatch
):
    from mcp.server.mcpserver.exceptions import ToolError

    from workgate.control.hosted_chat_session_tools import (
        NativeSessionDispatcher,
    )
    from workgate.protocol.ids import new_session_id

    harness, repo, lease, generation = bound
    _, sid = await native_session(harness, repo, lease)
    dispatcher = NativeSessionDispatcher(harness.control)
    write_scopes = {"shell:read", "shell:write"}
    path = tmp_path / "effect.txt"

    async def dispatch(call, scopes=write_scopes):
        return await dispatcher.dispatch(
            repo,
            lease,
            call,
            machine=harness.executor_id,
            scopes=scopes,
            expires=None,
        )

    def proposal(call_id, name, args):
        return ModelToolCall(call_id, name, {"session_id": sid} | args)

    write = proposal(
        "write-effect",
        "write_file",
        {"path": str(path), "content": "alpha\nbeta\n"},
    )
    with pytest.raises(PermissionError, match="shell:write"):
        await dispatch(write, {"shell:read"})
    with pytest.raises(PermissionError, match="does not belong"):
        await dispatch(
            ModelToolCall(
                "unowned-write",
                "write_file",
                write.arguments | {"session_id": new_session_id()},
            )
        )
    assert not path.exists()
    first = await dispatch(write)
    assert first["structuredContent"]["created"] is True
    assert path.read_text() == "alpha\nbeta\n"
    read = await dispatch(
        proposal("read-grounded", "read", {"path": str(path)})
    )
    snapshot = read["structuredContent"]["file"]["snapshot_id"]
    edit = proposal(
        "edit-effect",
        "edit_lines",
        {
            "path": str(path),
            "start_line": 1,
            "end_line": 1,
            "replacement": "ALPHA",
            "snapshot_id": snapshot,
        },
    )
    edited = await dispatch(edit)
    assert path.read_text() == "ALPHA\nbeta\n"
    assert await dispatch(edit) == edited
    assert await dispatch(write) == first
    assert path.read_text() == "ALPHA\nbeta\n"
    read = await dispatch(
        proposal("read-hashline", "read", {"path": str(path)})
    )
    snapshot = read["structuredContent"]["file"]["snapshot_id"]
    hashline = proposal(
        "hashline-effect",
        "hashline_edit",
        {
            "input": f"[{read['structuredContent']['path']}#{snapshot}]\n2:beta\n+BETA"
        },
    )
    changed = await dispatch(hashline)
    assert changed["structuredContent"]["hunk_count"] == 1
    assert path.read_text() == "ALPHA\nBETA\n"
    assert await dispatch(hashline) == changed

    if shutil.which("git"):
        patch = proposal(
            "patch-effect",
            "apply_patch",
            {
                "cwd": str(tmp_path),
                "patch": "diff --git a/effect.txt b/effect.txt\n--- a/effect.txt\n+++ b/effect.txt\n@@ -1,2 +1,2 @@\n ALPHA\n-BETA\n+GAMMA\n",
            },
        )
        result = await dispatch(patch)
        assert result["structuredContent"]["applied"] is True
        assert await dispatch(patch) == result
        assert path.read_text() == "ALPHA\nGAMMA\n"

    native = harness.control.executor_transport.call

    async def lose_reply(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        response = await native(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )
        if op == "write_file":
            raise TimeoutError("lost native write response")
        return response

    monkeypatch.setattr(harness.control.executor_transport, "call", lose_reply)
    uncertain = proposal(
        "lost-write", "write_file", {"path": str(path), "content": "once"}
    )
    with pytest.raises(ToolError, match="tool timeout"):
        await dispatch(uncertain)
    assert path.read_text() == "once"
    monkeypatch.setattr(harness.control.executor_transport, "call", native)
    path.write_text("later")
    with pytest.raises(NativeCreationUncertain, match="do not resend"):
        await dispatch(uncertain)
    assert path.read_text() == "later"
    delete = proposal(
        "delete-effect", "delete_file_or_dir", {"path": str(path)}
    )
    deleted = await dispatch(delete)
    assert not path.exists()
    assert await dispatch(delete) == deleted
    policy = "agent.machine_policy.conversation." + generation.conversation_id
    WorkspaceRepository(repo.database).save_setting(
        lease.owner, policy, {harness.executor_id: "read"}
    )
    with pytest.raises(PermissionError, match="read-only"):
        await dispatch(write)
    assert not path.exists()


@pytest.mark.asyncio
async def test_bounded_command_mode_and_replay(bound):
    from workgate.control.hosted_chat_session_tools import (
        NativeSessionDispatcher,
    )

    harness, repo, lease, _ = bound
    _, sid = await native_session(harness, repo, lease)
    dispatcher = NativeSessionDispatcher(harness.control)
    call = ModelToolCall(
        "bounded-echo",
        "bash",
        {"session_id": sid, "command": "echo bounded-output"},
    )

    async def dispatch(proposal, scopes=SCOPES):
        return await dispatcher.dispatch(
            repo,
            lease,
            proposal,
            machine=harness.executor_id,
            scopes=scopes,
            expires=None,
        )

    with pytest.raises(PermissionError, match="shell:execute"):
        await dispatch(call, {"shell:read", "shell:write"})
    for flag in ("async_", "pty", "persist_on_timeout"):
        with pytest.raises(ValueError, match="requires native reservation"):
            await dispatch(
                ModelToolCall(
                    "mode-" + flag, call.name, call.arguments | {flag: True}
                )
            )
    result = await dispatch(call)
    assert result["structuredContent"]["mode"] == "command"
    assert "bounded-output" in result["structuredContent"]["result"]["stdout"]
    assert await dispatch(call) == result
    python = ModelToolCall(
        "bounded-print",
        "run_python_code",
        {"session_id": sid, "code": "print('bounded-python')"},
    )
    result = await dispatch(python)
    assert result["structuredContent"]["mode"] == "command"
    assert "bounded-python" in result["structuredContent"]["result"]["stdout"]
    assert await dispatch(python) == result
    assert {row["kind"] for row in intent_rows(repo).values()} == {
        "task",
        "session",
    }


@pytest.mark.asyncio
async def test_owned_task_metadata_and_plan(bound):
    from workgate.control.hosted_chat_task_tools import NativeTaskDispatcher
    from workgate.protocol.ids import new_task_id

    harness, repo, lease, _ = bound
    task_id, session_id = await native_session(harness, repo, lease)
    dispatcher = NativeTaskDispatcher(harness.control)

    async def dispatch(call_id, name, arguments, *, scopes=SCOPES):
        return await dispatcher.dispatch(
            repo,
            lease,
            ModelToolCall(call_id, name, arguments),
            scopes=scopes,
            expires=None,
        )

    async def task(call_id, action, **fields):
        return (
            await dispatch(
                call_id,
                "task",
                {"action": action, "task_id": task_id, **fields},
            )
        )["structuredContent"]

    foreign = await harness.control.session_coordinator.start_session(
        executor_id=harness.executor_id, task_id=task_id
    )
    assert foreign["session_id"] != session_id
    assert (await task("get-owned", "get"))["session_ids"] == [session_id]
    with pytest.raises(PermissionError, match="does not belong"):
        await dispatch(
            "other", "task", {"action": "get", "task_id": new_task_id()}
        )
    with pytest.raises(PermissionError, match="shell:write"):
        await dispatch(
            "write-scope",
            "task",
            {"action": "get", "task_id": task_id},
            scopes={"shell:read"},
        )
    with pytest.raises(ValueError):
        await task("forbidden-action", "create")
    first = await task("report-once", "report", summary="first")
    await task("report-later", "report", summary="second")
    assert await task("report-once", "report", summary="first") == first
    assert (await task("latest", "get"))["progress"]["summary"] == "second"
    with pytest.raises(PermissionError, match="cannot change"):
        await task("report-once", "report", summary="changed")

    steps = [
        {
            "id": "step-1",
            "content": "Check plan",
            "status": "pending",
            "priority": "medium",
        }
    ]
    plan = await dispatch(
        "plan", "task_plan", {"task_id": task_id, "steps": steps}
    )
    assert plan["structuredContent"]["plan"]["steps"] == steps
    todos = await dispatch(
        "todos", "read_todos", {"task_id": task_id}, scopes={"shell:read"}
    )
    assert todos["structuredContent"]["todos"] == steps
    updated = [steps[0] | {"status": "completed"}]
    assert (
        await dispatch(
            "todo-write", "write_todos", {"task_id": task_id, "todos": updated}
        )
    )["structuredContent"]["todos"] == updated
    assert (await task("blocked", "block"))["status"] == "blocked"
    assert (await task("resume", "resume"))["status"] == "active"


@pytest.mark.asyncio
async def test_owned_native_session_lifecycle_recovers_close(
    bound, tmp_path, monkeypatch, tracked_shells
):
    from workgate.control.hosted_chat_cancellation import close_native_session
    from workgate.control.hosted_chat_session_tools import (
        NativeSessionDispatcher,
    )
    from workgate.protocol.executor import SESSION_TERMINATE_OP

    harness, repo, lease, _ = bound
    task_id, sid = await native_session(harness, repo, lease)
    foreign = await harness.control.session_coordinator.start_session(
        executor_id=harness.executor_id
    )
    dispatcher = NativeSessionDispatcher(harness.control)
    project = tmp_path / "project"
    change = ModelToolCall(
        "workdir-1",
        "session_change_workdir",
        {"session_id": sid, "workdir": str(project)},
    )

    async def change_dir(call):
        return await dispatcher.dispatch(
            repo,
            lease,
            call,
            machine=harness.executor_id,
            scopes={"shell:read"},
            expires=None,
        )

    first = await change_dir(change)
    assert first["structuredContent"]["session_id"] == sid
    assert first["structuredContent"]["workdir"] == str(project)
    await change_dir(
        ModelToolCall(
            "workdir-2",
            change.name,
            {"session_id": sid, "workdir": str(tmp_path)},
        )
    )
    assert await change_dir(change) == first
    assert harness.control.control_state.snapshot_sessions()[
        sid
    ].workdir == str(tmp_path)
    with pytest.raises(PermissionError, match="does not belong"):
        await change_dir(
            ModelToolCall(
                "other-dir",
                change.name,
                change.arguments | {"session_id": foreign["session_id"]},
            )
        )

    child = await invoke(
        harness,
        repo,
        lease,
        ModelToolCall(
            "session-child",
            "bash",
            {"session_id": sid, "command": "tracked child", "async_": True},
        ),
    )
    assert child["mode"] == "job"
    close = ModelToolCall("end-owned", "session_end", {"session_id": sid})

    async def end(call, scopes=SCOPES):
        return await close_native_session(
            harness.control,
            repo,
            lease,
            call,
            machine=harness.executor_id,
            scopes=scopes,
            expires=None,
        )

    with pytest.raises(PermissionError, match="shell:execute"):
        await end(close, {"shell:read", "shell:write"})
    with pytest.raises(ValueError, match="force is unsupported"):
        await end(
            ModelToolCall(
                "force-end", close.name, close.arguments | {"force": True}
            )
        )
    with pytest.raises(PermissionError, match="does not belong"):
        await end(
            ModelToolCall(
                "foreign-end", close.name, {"session_id": foreign["session_id"]}
            )
        )
    assert intent_rows(repo)["native-session"]["state"] == "active"
    native = harness.control.executor_transport.call
    terminate_calls = []

    async def lost_response(
        executor_id, op, args=None, *, session_id=None, timeout_s=None
    ):
        if op == SESSION_TERMINATE_OP:
            assert session_id == sid
            assert (
                intent_rows(repo)["native-session"]["state"] == "cancel_pending"
            )
            assert (
                intent_rows(repo)["session-child"]["state"] == "cancel_pending"
            )
            terminate_calls.append(session_id)
        response = await native(
            executor_id, op, args, session_id=session_id, timeout_s=timeout_s
        )
        if op == SESSION_TERMINATE_OP:
            raise ExecutorTransportError(
                ProtocolErrorCode.EXECUTOR_OFFLINE,
                "lost termination response",
                delivery_state="offered",
            )
        return response

    monkeypatch.setattr(
        harness.control.executor_transport, "call", lost_response
    )
    with pytest.raises(ExecutorTransportError):
        await end(close)
    assert intent_rows(repo)["native-session"]["state"] == "cancel_pending"
    assert harness.executor.sessions.lookup(sid) is None
    with pytest.raises(PermissionError, match="does not belong"):
        await change_dir(
            ModelToolCall("after-close", change.name, change.arguments)
        )
    monkeypatch.setattr(harness.control.executor_transport, "call", native)
    result = await end(close)
    assert result == {
        "session_id": sid,
        "executor_id": harness.executor_id,
        "ended": True,
        "force_released": False,
    }
    assert await end(close) == result
    assert terminate_calls == [sid]
    assert await sweep_native_cancellations(harness.control, repo) >= 1
    assert intent_rows(repo)["session-child"]["state"] == "stopped"
    assert intent_rows(repo)["native-session"]["state"] == "stopped"
    assert harness.executor.sessions.lookup(foreign["session_id"]) is not None
    assert (
        await harness.control.task_service.read_task(task_id)
    ).status == "active"
    with pytest.raises(PermissionError, match="cannot change"):
        await end(
            ModelToolCall(
                close.call_id, close.name, {"session_id": foreign["session_id"]}
            )
        )


@pytest.mark.asyncio
async def test_owned_task_terminal_transitions_and_confirmed_deletion(bound):
    from mcp.server.mcpserver.exceptions import ToolError

    from workgate.control.hosted_chat_task_tools import NativeTaskDispatcher
    from workgate.protocol.ids import new_task_id

    harness, repo, lease, _ = bound
    task_id, sid = await native_session(harness, repo, lease)
    dispatcher = NativeTaskDispatcher(harness.control)

    async def dispatch(call, scopes=SCOPES):
        return await dispatcher.dispatch(
            repo,
            lease,
            call,
            scopes=scopes,
            expires=None,
        )

    def task(call_id, action, **fields):
        return ModelToolCall(
            call_id,
            "task",
            {
                "action": action,
                "task_id": task_id,
                **fields,
            },
        )

    async def operation(call_id, action, **fields):
        return (await dispatch(task(call_id, action, **fields)))[
            "structuredContent"
        ]

    with pytest.raises(PermissionError, match="does not belong"):
        await dispatch(task("foreign-delete", "delete", task_id=new_task_id()))
    with pytest.raises((ToolError, NativeCreationUncertain)):
        await operation("delete-active", "delete")
    steps = [{"id": "one", "content": "Finish work", "status": "pending"}]
    await dispatch(
        ModelToolCall(
            "unfinished", "task_plan", {"task_id": task_id, "steps": steps}
        )
    )
    with pytest.raises((ToolError, NativeCreationUncertain)):
        await operation("finish-unfinished", "finish")
    await dispatch(
        ModelToolCall(
            "finished",
            "task_plan",
            {"task_id": task_id, "steps": [steps[0] | {"status": "completed"}]},
        )
    )
    finished = await operation("finish-owned", "finish")
    assert finished["status"] == "completed"
    assert harness.executor.sessions.lookup(sid) is not None
    assert (await operation("resume-completed", "resume"))["status"] == "active"
    assert await operation("finish-owned", "finish") == finished
    assert (await operation("get-resumed", "get"))["status"] == "active"
    assert (await operation("cancel-owned", "cancel"))["status"] == "cancelled"
    assert harness.executor.sessions.lookup(sid) is not None
    delete = task("delete-terminal", "delete")
    result = await dispatch(delete)
    assert result["structuredContent"] == {"task_id": task_id, "deleted": True}
    assert intent_rows(repo)["native-task"]["state"] == "stopped"
    assert (
        harness.control.control_state.snapshot_sessions()[sid].task_id is None
    )
    assert harness.executor.sessions.lookup(sid) is not None
    assert await dispatch(delete) == result
    with pytest.raises(PermissionError, match="shell:write"):
        await dispatch(delete, {"shell:read"})
    with pytest.raises(PermissionError, match="does not belong"):
        await operation("get-deleted", "get")
    with pytest.raises(PermissionError, match="does not belong"):
        await invoke(
            harness,
            repo,
            lease,
            ModelToolCall(
                "new-session-deleted-task",
                "session_start",
                {"executor_id": harness.executor_id, "task_id": task_id},
            ),
        )
    with pytest.raises(PermissionError, match="cannot change"):
        await dispatch(task(delete.call_id, "delete", task_id=new_task_id()))


@pytest.mark.asyncio
async def test_owned_task_delete_lost_response_is_not_reoffered(
    bound, monkeypatch
):
    from mcp.server.mcpserver.exceptions import ToolError

    from workgate.control.hosted_chat_task_tools import NativeTaskDispatcher

    harness, repo, lease, _ = bound
    task = await invoke(
        harness,
        repo,
        lease,
        ModelToolCall("task-to-remove", "task", {"action": "create"}),
    )
    task_id = task["task_id"]
    dispatcher = NativeTaskDispatcher(harness.control)
    service = harness.control.task_service
    await service.finish_task(task_id)
    original = service.delete_task
    called = []

    async def lost_response(identity):
        called.append(identity)
        await original(identity)
        raise TimeoutError("lost native Task deletion response")

    monkeypatch.setattr(service, "delete_task", lost_response)
    call = ModelToolCall(
        "lost-delete", "task", {"action": "delete", "task_id": task_id}
    )

    async def dispatch():
        return await dispatcher.dispatch(
            repo, lease, call, scopes=SCOPES, expires=None
        )

    with pytest.raises((ToolError, NativeCreationUncertain)):
        await dispatch()
    with pytest.raises(NativeCreationUncertain, match="do not resend"):
        await dispatch()
    assert called == [task_id]
    assert intent_rows(repo)["task-to-remove"]["state"] == "active"
    with repo.database.connect() as db:
        assert (
            db.execute(
                "SELECT receipt FROM agent_native_effect_calls WHERE call_id=?",
                (call.call_id,),
            ).fetchone()[0]
            is None
        )


@pytest.mark.asyncio
async def test_native_dispatch_composes_only_selected_owned_adapters(
    bound, tmp_path, tracked_shells, reserved_shell_backend
):
    from workgate.control.hosted_chat_native_dispatch import (
        NATIVE_TOOLS,
        NativeToolDispatcher,
    )

    harness, repo, lease, _ = bound
    dispatcher = NativeToolDispatcher(harness.control)

    async def dispatch(
        call_id,
        name,
        arguments,
        *,
        machine=None,
        tools=NATIVE_TOOLS,
        scopes=SCOPES,
    ):
        result = await dispatcher.dispatch(
            repo,
            lease,
            ModelToolCall(call_id, name, arguments),
            machine=machine,
            allowed_tools=tools,
            scopes=scopes,
            expires=None,
        )
        assert result["content"]
        return result["structuredContent"]

    with pytest.raises(PermissionError, match="not selected"):
        await dispatch(
            "not-selected", "task", {"action": "create"}, tools=frozenset()
        )
    with pytest.raises(PermissionError, match="not selected"):
        await dispatch(
            "unadapted",
            "session_retention_prune",
            {},
            tools=frozenset({"session_retention_prune"}),
        )
    assert intent_rows(repo) == {}
    with pytest.raises(PermissionError, match="explicit Executor"):
        await dispatch("implicit-task", "task", {"action": "create"})
    task = await dispatch(
        "dispatch-task",
        "task",
        {"action": "create", "label": "dispatch"},
        machine=harness.executor_id,
    )
    tid = task["task_id"]
    assert (
        await dispatch(
            "dispatch-task",
            "task",
            {"action": "create", "label": "dispatch", "objective": None},
            machine=harness.executor_id,
        )
    )["task_id"] == tid
    assert (
        await dispatch(
            "dispatch-get", "task", {"action": "get", "task_id": tid}
        )
    )["task_id"] == tid
    with pytest.raises(ValueError, match="Control-owned"):
        await dispatch(
            "task-machine",
            "task",
            {"action": "get", "task_id": tid},
            machine=harness.executor_id,
        )

    async def session(call_id, name, args, **kwargs):
        return await dispatch(
            call_id, name, args, machine=harness.executor_id, **kwargs
        )

    start = {"executor_id": harness.executor_id, "task_id": tid}
    with pytest.raises(PermissionError, match="explicit Executor"):
        await dispatch("implicit-executor", "session_start", start)
    started = await session("dispatch-start", "session_start", start)
    sid = started["session_id"]
    assert (
        await session(
            "dispatch-start", "session_start", start | {"workdir": None}
        )
    )["session_id"] == sid

    async def shell(call_id, name, args, **kwargs):
        return await session(
            call_id, name, {"session_id": sid, **args}, **kwargs
        )

    for flags in ({"async_": 1}, {"pty": True, "async_": True}):
        with pytest.raises(ValueError, match="mode"):
            await shell(
                "invalid-mode", "bash", {"command": "echo ignored", **flags}
            )
    assert "invalid-mode" not in intent_rows(repo)
    result = await shell("command", "bash", {"command": "echo dispatch"})
    assert result["mode"] == "command"
    assert "dispatch" in result["result"]["stdout"]
    job = await shell(
        "tracked-dispatch", "bash", {"command": "tracked", "async_": True}
    )
    assert job["mode"] == "job"
    jid = job["result"]["job_id"]
    listing = await shell("dispatch-jobs", "job", {"list_jobs": True})
    assert [row["job_id"] for row in listing["jobs"]] == [jid]
    pty = await shell("pty-dispatch", "bash", {"command": "shell", "pty": True})
    assert pty["mode"] == "pty"
    shid = pty["result"]["shell_id"]
    result = await shell(
        "dispatch-kill", "kill_persistent_shell", {"shell_id": shid}
    )
    assert result["shell_id"] == shid and result["killed"] is True
    path = tmp_path / "dispatch.txt"
    result = await shell(
        "dispatch-file", "write_file", {"path": str(path), "content": "test"}
    )
    assert result["created"] is True
    assert path.read_text() == "test"
    with pytest.raises(PermissionError, match="shell:write"):
        await shell(
            "denied-file",
            "write_file",
            {"path": str(path), "content": "denied"},
            scopes={"shell:read"},
        )
    assert path.read_text() == "test"
    result = await shell("dispatch-close", "session_end", {})
    assert result["ended"] and not result["force_released"]
    assert intent_rows(repo)["dispatch-start"]["state"] == "stopped"
    assert (
        await dispatch(
            "dispatch-get-after", "task", {"action": "get", "task_id": tid}
        )
    )["status"] == "active"


@pytest.mark.asyncio
async def test_native_catalog_uses_live_machine_scope_and_selected_agent(bound):
    from workgate.control.hosted_chat_native_dispatch import (
        NATIVE_TOOLS,
        NativeToolDispatcher,
    )
    from workgate.hosted_chat.agents import AgentRepository

    harness, repo, lease, generation = bound
    dispatcher = NativeToolDispatcher(harness.control)
    machine = harness.executor_id
    settings = WorkspaceRepository(repo.database)
    policy_key = (
        "agent.machine_policy.conversation." + generation.conversation_id
    )

    async def definitions(
        actor=lease,
        scopes=SCOPES,
        target=machine,
        expires=None,
        allowed=NATIVE_TOOLS,
    ):
        return {
            tool["function"]["name"]: tool["function"]["parameters"]
            for tool in await dispatcher.definitions(
                repo,
                actor,
                allowed_tools=allowed,
                scopes=scopes,
                expires=expires,
                task_machine=target,
            )
        }

    full = await definitions()
    assert set(full) == NATIVE_TOOLS
    assert full["session_start"]["properties"]["executor_id"]["enum"] == [
        machine
    ]
    assert set(full["session_start"]["required"]) == {"executor_id", "task_id"}
    assert "force" not in full["session_end"]["properties"]
    assert "background" not in full["session_copy"]["properties"]
    assert "create" in full["task"]["properties"]["action"]["enum"]
    assert (
        "create"
        not in (await definitions(target=None))["task"]["properties"]["action"][
            "enum"
        ]
    )
    assert set(
        await definitions(allowed=frozenset({"read", "executor_list"}))
    ) == {"read"}
    assert await definitions(expires=1) == {}
    assert "bash" not in await definitions(scopes={"shell:read", "shell:write"})
    settings.save_setting(lease.owner, policy_key, {machine: "read"})
    readonly = await definitions()
    assert {
        "read",
        "job",
        "task",
        "session_start",
        "session_change_workdir",
    } <= readonly.keys()
    assert {
        "bash",
        "session_end",
        "session_copy",
        "write_file",
        "kill_persistent_shell",
    }.isdisjoint(readonly)
    assert {"cancel", "retry"}.isdisjoint(readonly["job"]["properties"])
    assert "task" not in await definitions(scopes={"shell:read"})
    settings.save_setting(lease.owner, policy_key, {machine: "none"})
    assert set(await definitions()) == {
        "task",
        "task_plan",
        "read_todos",
        "write_todos",
    }
    assert (
        "create"
        not in (await definitions())["task"]["properties"]["action"]["enum"]
    )
    assert (
        "cancel" in full["job"]["properties"]
    )  # Catalog copies must be independent.
    assert "create" in full["task"]["properties"]["action"]["enum"]

    agents = AgentRepository(repo.database)
    agent = agents.create(
        lease.owner,
        {
            "provider": "openai",
            "model": "agent-model",
            "tools": ["read", "bash"],
        },
    )
    next_run, _ = GenerationStore(repo.database).begin(
        lease.owner, {"text": "agent tools", "agent_id": agent["id"]}
    )
    next_lease = ActorLease.claim(repo.database, lease.owner, next_run.run_id)
    assert next_lease is not None
    assert set(await definitions(actor=next_lease)) == {
        "task",
        "task_plan",
        "read",
        "bash",
    }
    agents.update(lease.owner, agent["id"], {"tools": ["read"]})
    assert set(await definitions(actor=next_lease)) == {
        "task",
        "task_plan",
        "read",
    }
    with pytest.raises(PermissionError, match="live Agent"):
        await dispatcher.dispatch(
            repo,
            next_lease,
            ModelToolCall("removed", "bash", {"command": "echo unused"}),
            machine=machine,
            allowed_tools=NATIVE_TOOLS,
            scopes=SCOPES,
            expires=None,
        )


@pytest.mark.asyncio
async def test_model_batch_resolves_only_confirmed_owned_native_targets(
    bound, tmp_path, monkeypatch
):
    from workgate.control.hosted_chat_native_dispatch import (
        NATIVE_TOOLS,
        NativeToolDispatcher,
    )

    harness, repo, lease, _ = bound
    dispatcher = NativeToolDispatcher(harness.control)
    executor = harness.executor_id

    async def dispatch(call_id, name, args, *, target=None):
        result = await dispatcher.dispatch_model_call(
            repo,
            lease,
            ModelToolCall(call_id, name, args),
            allowed_tools=NATIVE_TOOLS,
            scopes=SCOPES,
            expires=None,
            task_machine=target,
        )
        return result["structuredContent"]

    def prepare(*calls):
        return dispatcher.prepare_model_batch(
            repo, lease, list(calls), allowed_tools=NATIVE_TOOLS
        )

    with pytest.raises(PermissionError, match="explicit Executor"):
        await dispatch("unbound-task", "task", {"action": "create"})
    task = await dispatch(
        "model-task", "task", {"action": "create"}, target=executor
    )
    task_id = task["task_id"]
    session = await dispatch(
        "model-start",
        "session_start",
        {"executor_id": executor, "task_id": task_id},
    )
    sid = session["session_id"]
    foreign = await harness.control.session_coordinator.start_session(
        executor_id=executor
    )
    other_sid = foreign["session_id"]
    path = tmp_path / "model-preflight.txt"
    write = ModelToolCall(
        "prepared-write",
        "write_file",
        {"session_id": sid, "path": str(path), "content": "prepared"},
    )
    with pytest.raises(PermissionError, match="does not belong"):
        prepare(
            write,
            ModelToolCall(
                "foreign-read",
                "read",
                {"session_id": other_sid, "path": str(path)},
            ),
        )
    assert not path.exists()
    with pytest.raises(ValueError, match="duplicate"):
        prepare(write, write)
    [(prepared, target)] = prepare(write)
    assert target == executor
    assert prepared.arguments == write.arguments
    prepared.arguments["content"] = "modified"
    assert write.arguments["content"] == "prepared"
    assert not path.exists()
    assert (await dispatch(write.call_id, write.name, write.arguments))[
        "created"
    ] is True
    assert path.read_text() == "prepared"

    original_lookup = harness.control.session_coordinator.require_session_status

    def no_native_lookup(*args, **kwargs):
        raise AssertionError(
            "Foreign Session must be rejected before native lookup"
        )

    monkeypatch.setattr(
        harness.control.session_coordinator,
        "require_session_status",
        no_native_lookup,
    )
    with pytest.raises(PermissionError, match="does not belong"):
        await dispatch(
            "foreign-copy",
            "session_copy",
            {
                "src_session_id": sid,
                "src_path": str(path),
                "dst_session_id": other_sid,
                "dst_path": str(path),
            },
        )
    monkeypatch.setattr(
        harness.control.session_coordinator,
        "require_session_status",
        original_lookup,
    )
    with pytest.raises(PermissionError, match="does not belong"):
        await dispatch("foreign-end", "session_end", {"session_id": other_sid})
    assert (
        await dispatch(
            "model-get", "task", {"action": "get", "task_id": task_id}
        )
    )["task_id"] == task_id
    closed = await dispatch("model-end", "session_end", {"session_id": sid})
    assert closed["ended"]
    assert (
        await dispatch("model-end", "session_end", {"session_id": sid})
        == closed
    )
    with pytest.raises(PermissionError, match="does not belong"):
        await dispatch(
            "after-end", "read", {"session_id": sid, "path": str(path)}
        )
    assert harness.executor.sessions.lookup(other_sid) is not None


@pytest.mark.asyncio
async def test_native_tool_node_runs_model_tools_cycle_and_reuses_receipts(
    bound, tmp_path
):
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.hosted_chat.agent_runtime import (
        AgentContext,
        AgentLoopState,
        run_agent_loop,
    )
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig

    harness, repo, lease, _ = bound
    checkpoint = OwnedLoopCheckpoint(lease)
    selected = {"task", "session_start", "write_file", "read", "session_end"}
    revoked = False

    def check_grant():
        if revoked:
            raise PermissionError("principal revoked")

    node = NativeLoopToolNode(
        harness.control,
        repo,
        checkpoint,
        allowed_tools=selected,
        scopes=SCOPES,
        expires=None,
        task_machine=harness.executor_id,
        check_grant=check_grant,
    )
    ctx = AgentContext(
        ProviderConfig("openai", "model", "https://example.invalid", "fake"), []
    )
    state = AgentLoopState(ctx, [{"role": "user", "content": "make file"}])
    path = tmp_path / "node-test.txt"
    turns = []
    rendered = []

    def receipt(message):
        return json.loads(
            message["content"].split("Structured content: ", 1)[1]
        )

    async def stream(model, messages, **kwargs):
        turns.append(len(turns) + 1)
        assert {
            item["function"]["name"] for item in kwargs["tools"]
        } == selected
        if turns[-1] == 1:
            yield ModelToolCall("node-task", "task", {"action": "create"})
        elif turns[-1] == 2:
            task_id = receipt(messages[-1])["task_id"]
            yield ModelToolCall(
                "node-session",
                "session_start",
                {"executor_id": harness.executor_id, "task_id": task_id},
            )
        elif turns[-1] == 3:
            sid = receipt(messages[-1])["session_id"]
            yield ModelToolCall(
                "node-write",
                "write_file",
                {
                    "session_id": sid,
                    "path": str(path),
                    "content": "native loop",
                },
            )
        else:
            assert "created" in messages[-1]["content"]
            assert path.read_text() == "native loop"
            yield "complete"

    async def save(loop, calls):
        await checkpoint.save(loop, calls)

    async def on_text(value):
        rendered.append(value)

    async def no_text(*args, **kwargs):
        raise AssertionError("native tools must be advertised")
        yield ""

    async def dispatch(calls, loop):
        result = await node.dispatch(calls, loop)
        if calls[0].call_id == "node-write":
            path.write_text("changed after completion")
            assert (
                await node.dispatch(calls, loop)
            ).messages == result.messages
            assert path.read_text() == "changed after completion"
            path.write_text("native loop")
        return result

    await run_agent_loop(
        state,
        stream_tools=stream,
        stream_text=no_text,
        dispatch=dispatch,
        on_text=on_text,
        save_state=save,
        before_model=node.refresh_tools,
        preflight_calls=node.preflight,
    )
    assert turns == [1, 2, 3, 4]
    assert rendered == ["complete"]
    assert state.completed and len(intent_rows(repo)) == 2
    assert path.read_text() == "native loop"
    revoked = True
    with pytest.raises(PermissionError, match="principal revoked"):
        await node.refresh_tools(state)


@pytest.mark.asyncio
async def test_native_tool_node_checks_current_selection_and_principal_on_replay(
    bound, tmp_path
):
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig

    harness, repo, original, _ = bound
    agents = AgentRepository(repo.database)
    agent = agents.create(
        original.owner,
        {"provider": "openai", "model": "agent-model", "tools": ["read"]},
    )
    generation, _ = GenerationStore(repo.database).begin(
        original.owner, {"text": "read", "agent_id": agent["id"]}
    )
    lease = ActorLease.claim(repo.database, original.owner, generation.run_id)
    assert lease is not None
    _, sid = await native_session(harness, repo, lease)
    checkpoint = OwnedLoopCheckpoint(lease)
    context = AgentContext(
        ProviderConfig("openai", "model", "https://example.invalid", "fake"),
        [],
        agent=agent,
    )
    state = AgentLoopState(context, [])

    def make_node(
        repository: DurableTaskRepository, expires: int | None = None
    ):
        return NativeLoopToolNode(
            harness.control,
            repository,
            checkpoint,
            allowed_tools={"read"},
            scopes=SCOPES,
            task_machine=harness.executor_id,
            expires=expires,
            check_grant=lambda: None,
        )

    with pytest.raises(PermissionError, match="checkpoint database"):
        make_node(DurableTaskRepository(SQLiteDatabase(tmp_path / "other")))
    expired = make_node(repo, 1)
    with pytest.raises(PermissionError, match="expired"):
        await expired.refresh_tools(state)
    node = make_node(repo)
    await node.refresh_tools(state)
    with pytest.raises(PermissionError, match="not advertised"):
        node.preflight([ModelToolCall("bad", "bash", {})], state)

    path = tmp_path / "owned-replay.txt"
    path.write_text("original")
    calls = node.preflight(
        [
            ModelToolCall(
                "read-once", "read", {"session_id": sid, "path": str(path)}
            )
        ],
        state,
    )
    state.pending_calls = calls
    state.tool_step_started = True
    await checkpoint.save(state, calls)
    result = await node.dispatch(calls, state)
    assert "original" in result.messages[0]["content"]
    path.write_text("changed")
    assert (await node.dispatch(calls, state)).messages == result.messages

    token = bind_oauth_claims(
        {
            "sub": lease.owner,
            "scope": "shell:read",
            "exp": int(time.time()) + 60,
        }
    )
    try:
        with pytest.raises(PermissionError, match="grant was reduced"):
            await node.dispatch(calls, state)
    finally:
        reset_oauth_claims(token)

    settings = WorkspaceRepository(repo.database)
    policy_key = (
        "agent.machine_policy.conversation." + generation.conversation_id
    )
    settings.save_setting(
        lease.owner, policy_key, {harness.executor_id: "none"}
    )
    with pytest.raises(PermissionError, match="disclosure grant was reduced"):
        await node.dispatch(calls, state)
    settings.save_setting(lease.owner, policy_key, {})
    assert (await node.dispatch(calls, state)).messages == result.messages

    agents.update(lease.owner, agent["id"], {"tools": []})
    with pytest.raises(PermissionError, match="no longer selected"):
        await node.dispatch(calls, state)


@pytest.mark.asyncio
async def test_foreground_loop_suspends_then_resumes_exact_native_tool_edge(
    bound,
):
    from workgate.control.hosted_chat_foreground_loop import (
        run_foreground_native_loop,
    )
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.hosted_chat.agent_runtime import AgentContext, AgentSuspended
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import (
        ModelFinish,
        ModelThought,
        ProviderConfig,
    )

    harness, repo, lease, generation = bound
    store = GenerationStore(repo.database)

    def node(actor):
        return NativeLoopToolNode(
            harness.control,
            repo,
            OwnedLoopCheckpoint(actor),
            allowed_tools={"task"},
            scopes=SCOPES,
            expires=None,
            task_machine=harness.executor_id,
            check_grant=lambda: None,
        )

    def context():
        return AgentContext(
            ProviderConfig(
                "openai", "model", "https://example.invalid", "fake"
            ),
            [],
        )

    history = [{"role": "user", "content": "do work"}]

    async def first_stream(*args, **kwargs):
        yield ModelThought("thinking", text="summary")
        yield "prefix "
        yield ModelToolCall("create-task", "task", {"action": "create"})
        yield ModelFinish("openai", "tool_calls")

    async def pause(calls, state):
        assert calls[0].call_id == "create-task"
        raise AgentSuspended("await approval")

    async def no_text(*args, **kwargs):
        raise AssertionError("Task should be available")
        yield ""

    with pytest.raises(AgentSuspended):
        await run_foreground_native_loop(
            node(lease),
            context(),
            history,
            authorize_batch=pause,
            stream_tools=first_stream,
            stream_text=no_text,
        )
    current = store.get(lease.owner, lease.run_id)
    assert current is not None and current.status == "running"
    assert not intent_rows(repo)
    lease.release()
    fresh = ActorLease.claim(repo.database, lease.owner, lease.run_id)
    assert fresh is not None
    approved = []

    async def approve(calls, state):
        approved.extend(call.call_id for call in calls)

    async def resumed_stream(model, messages, **kwargs):
        assert messages[-1]["role"] == "tool"
        assert "task_id" in messages[-1]["content"]
        yield "done"
        yield ModelFinish("openai", "stop")

    result = await run_foreground_native_loop(
        node(fresh),
        context(),
        history,
        authorize_batch=approve,
        stream_tools=resumed_stream,
        stream_text=no_text,
    )
    assert result.completed and approved == ["create-task"]
    assert len(intent_rows(repo)) == 1
    current = store.get(lease.owner, lease.run_id)
    assert current is not None and current.status == "complete"
    events = [event for _, event in store.events(lease.owner, lease.run_id)]
    assert [
        event["data"]["delta"]["content"]["text"]
        for event in events
        if event.get("event") == "on_message_delta"
    ] == ["prefix ", "done"]
    assert [
        event["data"]["delta"]["content"]["think"]
        for event in events
        if event.get("event") == "on_reasoning_delta"
    ] == ["summary"]
    assert events[-1]["responseMessage"]["text"] == "prefix done"


@pytest.mark.asyncio
async def test_foreground_loop_does_not_replay_uncheckpointed_public_stream(
    bound,
):
    from workgate.control.hosted_chat_foreground_loop import (
        ObservableStreamInterrupted,
        run_foreground_native_loop,
    )
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.hosted_chat.agent_runtime import AgentContext
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig

    harness, repo, lease, _ = bound
    store = GenerationStore(repo.database)

    def node(actor):
        return NativeLoopToolNode(
            harness.control,
            repo,
            OwnedLoopCheckpoint(actor),
            allowed_tools=set(),
            scopes=SCOPES,
            expires=None,
            task_machine=None,
            check_grant=lambda: None,
        )

    def context():
        return AgentContext(
            ProviderConfig(
                "openai", "model", "https://example.invalid", "fake"
            ),
            [],
        )

    async def interrupted(*args, **kwargs):
        yield "already visible"
        raise asyncio.CancelledError

    async def forbidden(*args):
        raise AssertionError("no tool should be offered")

    async def forbidden_stream(*args, **kwargs):
        raise AssertionError("visible stream must not be replayed")
        yield ""

    with pytest.raises(asyncio.CancelledError):
        await run_foreground_native_loop(
            node(lease),
            context(),
            [],
            authorize_batch=forbidden,
            stream_text=interrupted,
            stream_tools=forbidden_stream,
        )
    current = store.get(lease.owner, lease.run_id)
    assert current is not None and current.status == "running"
    lease.release()
    fresh = ActorLease.claim(repo.database, lease.owner, lease.run_id)
    assert fresh is not None
    with pytest.raises(ObservableStreamInterrupted):
        await run_foreground_native_loop(
            node(fresh),
            context(),
            [],
            authorize_batch=forbidden,
            stream_text=forbidden_stream,
            stream_tools=forbidden_stream,
        )
    current = store.get(lease.owner, lease.run_id)
    assert current is not None and current.status == "interrupted"
    events = [event for _, event in store.events(lease.owner, lease.run_id)]
    assert events[-1]["responseMessage"]["text"] == "already visible"
    assert "cannot replay" in events[-1]["error"]
    assert not intent_rows(repo)


def test_foreground_safe_generation_error_does_not_leak_provider_details():
    import httpx

    from workgate.control.hosted_chat_foreground_loop import (
        safe_generation_error,
    )

    request = httpx.Request("POST", "https://private.invalid/chat?token=secret")
    response = httpx.Response(503, request=request)
    error = safe_generation_error(
        httpx.HTTPStatusError("secret", request=request, response=response)
    )
    assert "HTTP 503" in error and "HTTPStatusError" in error
    assert "secret" not in error and "private" not in error
    assert "stream disconnected" in safe_generation_error(
        httpx.RemoteProtocolError("secret")
    )
    assert "secret" not in safe_generation_error(ValueError("secret"))


@pytest.mark.asyncio
async def test_native_approval_mixed_batch_edit_reject_and_replay(
    bound, tmp_path
):
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.hosted_chat.agent_runtime import (
        AgentContext,
        AgentLoopState,
        AgentSuspended,
    )
    from workgate.hosted_chat.approval_claims import ApprovalClaimStore
    from workgate.hosted_chat.approval_settings import save_mode
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig

    harness, repo, lease, generation = bound
    _, sid = await native_session(harness, repo, lease)
    prefs = WorkspaceRepository(repo.database)
    save_mode(
        prefs,
        lease.owner,
        "dangerous",
        conversation_id=generation.conversation_id,
    )
    claim_store = ApprovalClaimStore(repo.database)
    context = AgentContext(
        ProviderConfig("openai", "model", "https://example.invalid", "fake"), []
    )

    def node(actor):
        return NativeLoopToolNode(
            harness.control,
            repo,
            OwnedLoopCheckpoint(actor),
            allowed_tools={"read", "write_file"},
            scopes=SCOPES,
            expires=None,
            task_machine=harness.executor_id,
            check_grant=lambda: None,
        )

    current = node(lease)
    state = AgentLoopState(context, [])
    await current.refresh_tools(state)
    source = tmp_path / "read.txt"
    source.write_text("original")
    target = tmp_path / "edited.txt"
    refused = tmp_path / "rejected.txt"
    calls = current.preflight(
        [
            ModelToolCall(
                "read", "read", {"session_id": sid, "path": str(source)}
            ),
            ModelToolCall(
                "write",
                "write_file",
                {
                    "session_id": sid,
                    "path": str(target),
                    "content": "original proposal",
                },
            ),
            ModelToolCall(
                "reject",
                "write_file",
                {"session_id": sid, "path": str(refused), "content": "no"},
            ),
        ],
        state,
    )
    state.pending_calls = calls
    state.tool_step_started = True
    await current.checkpoint.save(state, calls)
    with pytest.raises(AgentSuspended, match="approval"):
        await current.dispatch(calls, state)
    pending = claim_store.pending(lease.owner, lease.run_id)
    assert pending is not None
    assert [
        request["tool_call_id"]
        for request in pending["payload"]["action_requests"]
    ] == ["write", "reject"]
    assert not target.exists() and not refused.exists()
    edited = {**calls[1].arguments, "content": "approved edit"}
    claim_store.claim(
        lease.owner,
        lease.run_id,
        pending["actionId"],
        [
            {
                "tool_call_id": "write",
                "decision": "edit",
                "editedArguments": edited,
            },
            {
                "tool_call_id": "reject",
                "decision": "reject",
                "reason": "not requested",
            },
        ],
    )
    replacement = ActorLease.claim(repo.database, lease.owner, lease.run_id)
    assert replacement is not None
    current = node(replacement)
    restored = current.checkpoint.restore(context)
    assert restored is not None
    inspected = []

    async def authorize(effects, state):
        inspected.extend(effects)

    result = await current.dispatch(
        calls, restored.state, authorize_effects=authorize
    )
    assert [message["tool_call_id"] for message in result.messages] == [
        "read",
        "write",
        "reject",
    ]
    assert "original" in result.messages[0]["content"]
    assert (
        result.messages[2]["is_error"]
        and "not requested" in result.messages[2]["content"]
    )
    assert target.read_text() == "approved edit" and not refused.exists()
    assert [call.call_id for call in inspected] == ["read", "write"]
    assert inspected[1].arguments == edited
    # Historical results are keyed to the original model proposal, not the edit.
    target.write_text("externally changed")
    source.write_text("different")
    replacement.release()
    again = ActorLease.claim(repo.database, lease.owner, lease.run_id)
    assert again is not None
    current = node(again)
    restored = current.checkpoint.restore(context)
    assert restored is not None
    inspected.clear()
    assert (
        await current.dispatch(
            calls, restored.state, authorize_effects=authorize
        )
    ).messages == result.messages
    assert target.read_text() == "externally changed" and not inspected


@pytest.mark.asyncio
async def test_native_approval_edit_foreign_target_blocks_siblings(
    bound, tmp_path
):
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.hosted_chat.agent_runtime import (
        AgentContext,
        AgentLoopState,
        AgentSuspended,
    )
    from workgate.hosted_chat.approval_claims import ApprovalClaimStore
    from workgate.hosted_chat.approval_settings import save_mode
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig

    harness, repo, lease, generation = bound
    _, sid = await native_session(harness, repo, lease)
    foreign_run, _ = GenerationStore(repo.database).begin(
        lease.owner, {"text": "another conversation"}
    )
    foreign = ActorLease.claim(repo.database, lease.owner, foreign_run.run_id)
    assert foreign is not None
    _, foreign_sid = await native_session(harness, repo, foreign)
    save_mode(
        WorkspaceRepository(repo.database),
        lease.owner,
        "all",
        conversation_id=generation.conversation_id,
    )
    store = ApprovalClaimStore(repo.database)
    context = AgentContext(
        ProviderConfig("openai", "model", "https://example.invalid", "fake"), []
    )

    def node(actor):
        return NativeLoopToolNode(
            harness.control,
            repo,
            OwnedLoopCheckpoint(actor),
            allowed_tools={"write_file"},
            scopes=SCOPES,
            expires=None,
            task_machine=harness.executor_id,
            check_grant=lambda: None,
        )

    current = node(lease)
    state = AgentLoopState(context, [])
    await current.refresh_tools(state)
    paths = [tmp_path / "first.txt", tmp_path / "second.txt"]
    calls = current.preflight(
        [
            ModelToolCall(
                str(i),
                "write_file",
                {"session_id": sid, "path": str(path), "content": "x"},
            )
            for i, path in enumerate(paths)
        ],
        state,
    )
    state.pending_calls = calls
    state.tool_step_started = True
    await current.checkpoint.save(state, calls)
    with pytest.raises(AgentSuspended):
        await current.dispatch(calls, state)
    pending = store.pending(lease.owner, lease.run_id)
    assert pending is not None
    store.claim(
        lease.owner,
        lease.run_id,
        pending["actionId"],
        [
            {"tool_call_id": "0", "decision": "approve"},
            {
                "tool_call_id": "1",
                "decision": "edit",
                "editedArguments": {
                    **calls[1].arguments,
                    "session_id": foreign_sid,
                },
            },
        ],
    )
    fresh = ActorLease.claim(repo.database, lease.owner, lease.run_id)
    assert fresh is not None
    current = node(fresh)
    restored = current.checkpoint.restore(context)
    assert restored is not None
    with pytest.raises(PermissionError, match="does not belong"):
        await current.dispatch(calls, restored.state)
    assert not any(path.exists() for path in paths)


@pytest.mark.parametrize(
    "change",
    ["initial_deny", "late_deny", "late_ask", "claimed_deny", "claimed_ask"],
)
@pytest.mark.asyncio
async def test_native_approval_live_policy_changes(bound, tmp_path, change):
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.hosted_chat.agent_runtime import (
        AgentContext,
        AgentLoopState,
        AgentSuspended,
    )
    from workgate.hosted_chat.approval_claims import ApprovalClaimStore
    from workgate.hosted_chat.approval_settings import save_mode
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig

    harness, repo, lease, generation = bound
    _, sid = await native_session(harness, repo, lease)
    prefs = WorkspaceRepository(repo.database)
    store = ApprovalClaimStore(repo.database)
    context = AgentContext(
        ProviderConfig("openai", "model", "https://example.invalid", "fake"), []
    )

    def node(actor):
        return NativeLoopToolNode(
            harness.control,
            repo,
            OwnedLoopCheckpoint(actor),
            allowed_tools={"write_file", "read"},
            scopes=SCOPES,
            expires=None,
            task_machine=harness.executor_id,
            check_grant=lambda: None,
        )

    def deny():
        prefs.save_setting(
            lease.owner,
            "endpoints",
            {
                "agents": {
                    "toolApproval": {
                        "enabled": True,
                        "mode": "bypass",
                        "deny": ["write_file"],
                        "reason": "Policy blocks {tool}",
                    }
                }
            },
        )

    def all_mode():
        save_mode(
            prefs,
            lease.owner,
            "all",
            conversation_id=generation.conversation_id,
        )

    current = node(lease)
    state = AgentLoopState(context, [])
    await current.refresh_tools(state)
    source = tmp_path / "read.txt"
    source.write_text("read")
    target = tmp_path / "write.txt"
    calls = current.preflight(
        [
            ModelToolCall(
                "write",
                "write_file",
                {"session_id": sid, "path": str(target), "content": "x"},
            ),
            ModelToolCall(
                "read", "read", {"session_id": sid, "path": str(source)}
            ),
        ],
        state,
    )
    state.pending_calls = calls
    state.tool_step_started = True
    await current.checkpoint.save(state, calls)

    if change.startswith("claimed"):
        save_mode(
            prefs,
            lease.owner,
            "dangerous",
            conversation_id=generation.conversation_id,
        )
        with pytest.raises(AgentSuspended):
            await current.dispatch(calls, state)
        pending = store.pending(lease.owner, lease.run_id)
        assert pending is not None
        store.claim(
            lease.owner,
            lease.run_id,
            pending["actionId"],
            [{"tool_call_id": "write", "decision": "approve"}],
        )
        fresh = ActorLease.claim(repo.database, lease.owner, lease.run_id)
        assert fresh is not None
        current = node(fresh)
        restored = current.checkpoint.restore(context)
        assert restored is not None
        state = restored.state
        if change == "claimed_deny":
            deny()
        else:
            all_mode()
    elif change == "initial_deny":
        deny()

    async def authorize(effects, state):
        if change == "late_deny":
            deny()
        elif change == "late_ask":
            all_mode()

    if change.endswith("ask"):
        with pytest.raises(PermissionError, match="policy changed"):
            await current.dispatch(calls, state, authorize_effects=authorize)
        saved = current.checkpoint.saver.get_tuple(current.checkpoint.active)
        assert saved is not None
        assert not any(
            channel == "tool_receipt"
            for _, channel, _ in saved["pendingWrites"]
        )
    else:
        result = await current.dispatch(
            calls, state, authorize_effects=authorize
        )
        assert result.messages[0]["is_error"]
        assert "Policy blocks write_file" in result.messages[0]["content"]
        assert not result.messages[1]["is_error"]
    assert not target.exists()


@pytest.mark.parametrize("abort", [True, False])
@pytest.mark.asyncio
async def test_pending_approval_terminal_preserves_native_cancellation_boundary(
    bound, abort
):
    from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
    from workgate.hosted_chat.approval_claims import ApprovalClaimStore
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig

    harness, repo, lease, generation = bound
    task_id, sid = await native_session(harness, repo, lease)
    unrelated, _ = GenerationStore(repo.database).begin(
        lease.owner, {"text": "separate"}
    )
    other = ActorLease.claim(repo.database, lease.owner, unrelated.run_id)
    assert other is not None
    other_task, other_sid = await native_session(harness, repo, other)

    checkpoint = OwnedLoopCheckpoint(lease)
    context = AgentContext(
        ProviderConfig("openai", "model", "https://example.invalid", "fake"), []
    )
    calls = [ModelToolCall("review", "session_end", {"session_id": sid})]
    state = AgentLoopState(
        context, [], pending_calls=calls, tool_step_started=True
    )
    await checkpoint.save(state, calls)
    store = ApprovalClaimStore(repo.database)
    pending = store.pause(checkpoint, calls)
    if abort:
        assert store.abort_pending(
            lease.owner, lease.run_id, pending["actionId"]
        )
    else:
        with repo.database.connect() as db:
            db.execute(
                "UPDATE agent_checkpoints SET expires_at=0 WHERE checkpoint_ns=?",
                (lease.run_id,),
            )
        assert store.expire_pending(
            lease.owner, lease.run_id, pending["actionId"]
        )
    with repo.database.read_snapshot() as db:
        rows = db.execute(
            "SELECT run_id,state FROM agent_durable_tasks"
        ).fetchall()
    owned = [row for row in rows if row["run_id"] == lease.run_id]
    foreign = [row for row in rows if row["run_id"] == unrelated.run_id]
    assert len(owned) == len(foreign) == 2
    assert {row["state"] for row in owned} == (
        {"cancel_pending"} if abort else {"active"}
    )
    assert {row["state"] for row in foreign} == {"active"}
    assert repo.is_run_cancelled(lease.run_id) == abort
    assert not repo.is_run_cancelled(unrelated.run_id)
    # Pending-review finalization records cleanup intent; it does not execute a native stop.
    assert harness.executor.sessions.lookup(sid) is not None
    assert harness.executor.sessions.lookup(other_sid) is not None
    assert task_id != other_task


@pytest.mark.asyncio
async def test_foreground_ask_user_without_machine_grants_resumes_exact_answer(
    bound,
):
    from workgate.control.hosted_chat_foreground_loop import (
        run_foreground_native_loop,
    )
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.hosted_chat.agent_runtime import AgentContext, AgentSuspended
    from workgate.hosted_chat.approval_claims import ApprovalClaimStore
    from workgate.hosted_chat.approval_settings import save_mode
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig

    harness, repo, lease, generation = bound
    store = ApprovalClaimStore(repo.database)
    save_mode(
        WorkspaceRepository(repo.database),
        lease.owner,
        "all",
        conversation_id=generation.conversation_id,
    )

    def node(actor):
        return NativeLoopToolNode(
            harness.control,
            repo,
            OwnedLoopCheckpoint(actor),
            allowed_tools={"ask_user_question"},
            scopes=set(),
            expires=None,
            task_machine=None,
            check_grant=lambda: None,
        )

    def context():
        return AgentContext(
            ProviderConfig(
                "openai", "model", "https://example.invalid", "fake"
            ),
            [],
        )

    async def authorize(calls, state):
        assert len(calls) == 1 and calls[0].name == "ask_user_question"

    async def first(model, messages, **kwargs):
        assert [tool["function"]["name"] for tool in kwargs["tools"]] == [
            "ask_user_question"
        ]
        yield "Please choose. "
        yield ModelToolCall(
            "question",
            "ask_user_question",
            {"questions": [{"id": "format", "question": "Which format?"}]},
        )

    async def no_text(*args, **kwargs):
        raise AssertionError("Question must be advertised")
        yield ""

    with pytest.raises(AgentSuspended, match="Ask User"):
        await run_foreground_native_loop(
            node(lease),
            context(),
            [],
            authorize_batch=authorize,
            stream_tools=first,
            stream_text=no_text,
        )
    pending = store.pending(lease.owner, lease.run_id)
    assert (
        pending is not None
        and pending["payload"]["type"] == "ask_user_question"
    )
    store.claim_question(
        lease.owner,
        lease.run_id,
        pending["actionId"],
        {"answers": {"format": "JSON"}},
    )
    fresh = ActorLease.claim(repo.database, lease.owner, lease.run_id)
    assert fresh is not None

    async def second(model, messages, **kwargs):
        assert messages[-1]["tool_call_id"] == "question"
        assert json.loads(messages[-1]["content"]) == {
            "answers": {"format": "JSON"}
        }
        yield "Done."

    result = await run_foreground_native_loop(
        node(fresh),
        context(),
        [],
        authorize_batch=authorize,
        stream_tools=second,
        stream_text=no_text,
    )
    assert result.completed
    events = [
        event
        for _, event in GenerationStore(repo.database).events(
            lease.owner, lease.run_id
        )
    ]
    assert (
        sum(event.get("event") == "on_pending_action" for event in events) == 1
    )
    assert events[-1]["responseMessage"]["text"] == "Please choose. Done."
    assert not intent_rows(repo)


@pytest.mark.asyncio
async def test_ask_user_explicit_review_edit_then_answer_and_receipt_replay(
    bound,
):
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.hosted_chat.agent_runtime import (
        AgentContext,
        AgentLoopState,
        AgentSuspended,
    )
    from workgate.hosted_chat.approval_claims import ApprovalClaimStore
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig

    harness, repo, lease, _ = bound
    store = ApprovalClaimStore(repo.database)
    WorkspaceRepository(repo.database).save_setting(
        lease.owner,
        "endpoints",
        {
            "agents": {
                "toolApproval": {"enabled": True, "ask": ["ask_user_question"]}
            }
        },
    )
    context = AgentContext(
        ProviderConfig("openai", "model", "https://example.invalid", "fake"), []
    )

    def node(actor):
        return NativeLoopToolNode(
            harness.control,
            repo,
            OwnedLoopCheckpoint(actor),
            allowed_tools={"ask_user_question"},
            scopes=set(),
            expires=None,
            task_machine=None,
            check_grant=lambda: None,
        )

    current = node(lease)
    state = AgentLoopState(context, [])
    await current.refresh_tools(state)
    calls = current.preflight(
        [ModelToolCall("q", "ask_user_question", {"question": "Original?"})],
        state,
    )
    state.pending_calls = calls
    state.tool_step_started = True
    await current.checkpoint.save(state, calls)
    with pytest.raises(AgentSuspended, match="approval"):
        await current.dispatch(calls, state)
    pending = store.pending(lease.owner, lease.run_id)
    assert pending is not None and pending["payload"]["type"] == "tool_approval"
    store.claim(
        lease.owner,
        lease.run_id,
        pending["actionId"],
        [
            {
                "tool_call_id": "q",
                "decision": "edit",
                "editedArguments": {"question": "Edited?"},
            }
        ],
    )

    def recover():
        actor = ActorLease.claim(repo.database, lease.owner, lease.run_id)
        assert actor is not None
        restored_node = node(actor)
        recovered = restored_node.checkpoint.restore(context)
        assert recovered is not None
        return restored_node, recovered.state

    current, state = recover()
    with pytest.raises(AgentSuspended, match="Ask User"):
        await current.dispatch(calls, state)
    pending = store.pending(lease.owner, lease.run_id)
    assert (
        pending is not None
        and pending["payload"]["question"]["question"] == "Edited?"
    )
    store.claim_question(
        lease.owner, lease.run_id, pending["actionId"], {"answer": "answer"}
    )
    current, state = recover()
    result = await current.dispatch(calls, state)
    assert json.loads(result.messages[0]["content"]) == {"answer": "answer"}
    current.checkpoint.lease.release()
    current, state = recover()
    assert (await current.dispatch(calls, state)).messages == result.messages
    assert not intent_rows(repo)


@pytest.mark.asyncio
async def test_ask_user_requires_live_opt_in_and_lone_valid_proposal(bound):
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig

    harness, repo, original, _ = bound
    agents = AgentRepository(repo.database)
    agent = agents.create(
        original.owner, {"provider": "openai", "model": "m", "tools": []}
    )
    generation, _ = GenerationStore(repo.database).begin(
        original.owner, {"text": "question", "agent_id": agent["id"]}
    )
    lease = ActorLease.claim(repo.database, original.owner, generation.run_id)
    assert lease is not None
    node = NativeLoopToolNode(
        harness.control,
        repo,
        OwnedLoopCheckpoint(lease),
        allowed_tools={"ask_user_question", "task"},
        scopes=SCOPES,
        expires=None,
        task_machine=harness.executor_id,
        check_grant=lambda: None,
    )
    state = AgentLoopState(
        AgentContext(
            ProviderConfig("openai", "m", "https://example.invalid", "fake"),
            [],
            agent=agent,
        ),
        [],
    )
    question = ModelToolCall("q", "ask_user_question", {"question": "Choose?"})
    await node.refresh_tools(state)
    assert "ask_user_question" not in {
        tool["function"]["name"] for tool in state.context.tools
    }
    with pytest.raises(PermissionError, match="not advertised"):
        node.preflight([question], state)
    agents.update(lease.owner, agent["id"], {"tools": ["ask_user_question"]})
    await node.refresh_tools(state)
    with pytest.raises(ValueError, match="parallel"):
        node.preflight(
            [question, ModelToolCall("task", "task", {"action": "create"})],
            state,
        )
    with pytest.raises(ValueError, match="Ask User"):
        node.preflight([ModelToolCall("bad", "ask_user_question", {})], state)
    calls = node.preflight([question], state)
    state.pending_calls = calls
    state.tool_step_started = True
    await node.checkpoint.save(state, calls)
    agents.update(lease.owner, agent["id"], {"tools": []})
    with pytest.raises(PermissionError, match="no longer selected"):
        node.preflight(calls, state)
    with pytest.raises(PermissionError, match="no longer selected"):
        await node.dispatch(calls, state)
    assert not intent_rows(repo)


@pytest.mark.parametrize("preempt", [True, False])
@pytest.mark.asyncio
async def test_foreground_steering_preserves_fifo_and_safe_tool_boundary(
    bound, preempt
):
    from workgate.control.hosted_chat_foreground_loop import (
        run_foreground_native_loop,
    )
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.hosted_chat.agent_runtime import AgentContext
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig
    from workgate.hosted_chat.steering import SteeringStore

    harness, repo, lease, generation = bound
    steering = SteeringStore(repo.database)
    context = AgentContext(
        ProviderConfig("openai", "model", "https://example.invalid", "fake"), []
    )
    node = NativeLoopToolNode(
        harness.control,
        repo,
        OwnedLoopCheckpoint(lease),
        allowed_tools={"task"},
        scopes=SCOPES,
        expires=None,
        task_machine=harness.executor_id,
        check_grant=lambda: None,
    )
    calls = 0
    queued = []

    def submit(text, armed):
        ack = steering.enqueue(
            lease.owner,
            {
                "conversationId": generation.conversation_id,
                "generationCreatedAt": generation.generation_created_at,
                "text": text,
                "preempt": armed,
            },
            preempt_capable=True,
            validate_submission=lambda text: None,
        )
        queued.append(ack["steerId"])

    async def stream(model, messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield "prefix "
            yield ModelToolCall("task", "task", {"action": "create"})
            submit("first", False)
            submit("second", preempt)
        else:
            assert calls == 2
            assert [
                message["content"]
                for message in messages
                if message.get("source") == "steer"
            ] == ["first", "second"]
            tool_messages = [
                message for message in messages if message["role"] == "tool"
            ]
            assert bool(tool_messages) is not preempt
            yield "done"

    async def authorize(calls, state):
        assert not preempt

    async def no_text(*args, **kwargs):
        raise AssertionError("Task should be advertised")
        yield ""

    state = await run_foreground_native_loop(
        node,
        context,
        [{"role": "user", "content": "start"}],
        authorize_batch=authorize,
        stream_tools=stream,
        stream_text=no_text,
    )
    assert state.completed and calls == 2
    assert bool(intent_rows(repo)) is not preempt
    events = [
        event
        for _, event in GenerationStore(repo.database).events(
            lease.owner, lease.run_id
        )
    ]
    assert [
        event["data"]["steerId"]
        for event in events
        if event.get("event") == "on_steer_applied"
    ] == queued
    assert [
        event["data"]["index"]
        for event in events
        if event.get("event") == "on_steer_applied"
    ] == [1, 2]
    final = events[-1]["responseMessage"]
    assert final["text"] == "prefix done"
    assert [part["type"] for part in final["content"]] == [
        "text",
        "steer",
        "steer",
        "text",
    ]


@pytest.mark.parametrize("with_tool", [False, True])
@pytest.mark.asyncio
async def test_foreground_steering_recovers_committed_delivery_without_replay(
    bound, monkeypatch, with_tool
):
    from workgate.control.hosted_chat_foreground_loop import (
        run_foreground_native_loop,
    )
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.hosted_chat.agent_runtime import AgentContext
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig
    from workgate.hosted_chat.steering import SteeringStore

    harness, repo, lease, generation = bound
    steering = SteeringStore(repo.database)

    def make_node(actor):
        return NativeLoopToolNode(
            harness.control,
            repo,
            OwnedLoopCheckpoint(actor),
            allowed_tools={"task"},
            scopes=SCOPES,
            expires=None,
            task_machine=harness.executor_id,
            check_grant=lambda: None,
        )

    def context():
        return AgentContext(
            ProviderConfig(
                "openai", "model", "https://example.invalid", "fake"
            ),
            [],
        )

    seen = []

    async def authorize(calls, state):
        seen.extend(call.call_id for call in calls)

    ack = steering.enqueue(
        lease.owner,
        {
            "conversationId": generation.conversation_id,
            "text": "redirect",
            "clientSteerId": "once",
        },
        preempt_capable=True,
        validate_submission=lambda text: None,
    )

    async def first(*args, **kwargs):
        yield "prefix "
        if with_tool:
            yield ModelToolCall("task", "task", {"action": "create"})

    async def no_text(*args, **kwargs):
        raise AssertionError("Task should be advertised")
        yield ""

    drain = SteeringStore.drain

    def crash_after_drain(self, actor, *, boundary):
        applied = drain(self, actor, boundary=boundary)
        if applied:
            raise asyncio.CancelledError()
        return applied

    monkeypatch.setattr(SteeringStore, "drain", crash_after_drain)
    with pytest.raises(asyncio.CancelledError):
        await run_foreground_native_loop(
            make_node(lease),
            context(),
            [],
            authorize_batch=authorize,
            stream_tools=first,
            stream_text=no_text,
        )
    assert steering.applied_parts(lease)[0]["steerId"] == ack["steerId"]
    assert bool(intent_rows(repo)) is with_tool
    assert seen == (["task"] if with_tool else [])
    lease.release()
    fresh = ActorLease.claim(repo.database, lease.owner, lease.run_id)
    assert fresh is not None
    monkeypatch.setattr(SteeringStore, "drain", drain)

    async def second(model, messages, **kwargs):
        assert messages[-1]["steerId"] == ack["steerId"]
        assert (
            len(
                [
                    message
                    for message in messages
                    if message.get("source") == "steer"
                ]
            )
            == 1
        )
        assert len(
            [message for message in messages if message["role"] == "tool"]
        ) == int(with_tool)
        yield "done"

    state = await run_foreground_native_loop(
        make_node(fresh),
        context(),
        [],
        authorize_batch=authorize,
        stream_tools=second,
        stream_text=no_text,
    )
    assert state.completed
    assert seen == (["task"] if with_tool else [])
    events = [
        event
        for _, event in GenerationStore(repo.database).events(
            lease.owner, lease.run_id
        )
    ]
    assert (
        sum(event.get("event") == "on_steer_applied" for event in events) == 1
    )
    assert events[-1]["responseMessage"]["text"] == "prefix done"


@pytest.mark.asyncio
async def test_graph_native_activation_identity_and_live_member_ceiling(bound):
    from dataclasses import replace
    from uuid import uuid4

    from workgate.control.hosted_chat_native_dispatch import (
        NATIVE_TOOLS,
        NativeToolDispatcher,
    )
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.graph_runtime import (
        GraphMemberResult,
        run_multi_agent_graph,
    )

    pytest.importorskip("langgraph")
    harness, original, lease, _ = bound
    agents = AgentRepository(original.database)
    member = agents.create(
        lease.owner,
        {"provider": "openai", "model": "member", "tools": ["read", "bash"]},
    )
    root = agents.create(
        lease.owner,
        {"provider": "openai", "model": "root", "tools": ["read"]},
    )
    agents.update(
        lease.owner,
        root["id"],
        {
            "edges": [
                {"from": root["id"], "to": member["id"], "edgeType": "direct"}
            ]
        },
    )
    generation, _ = GenerationStore(original.database).begin(
        lease.owner, {"text": "graph", "agent_id": root["id"]}
    )
    actor = ActorLease.claim(original.database, lease.owner, generation.run_id)
    assert actor is not None
    dispatcher = NativeToolDispatcher(harness.control)
    created = {}

    async def execute(activation):
        repo = DurableTaskRepository(original.database, scope=activation.scope)
        call = ModelToolCall("same-provider-id", "task", {"action": "create"})
        names = {
            tool["function"]["name"]
            for tool in await dispatcher.definitions(
                repo,
                actor,
                allowed_tools=NATIVE_TOOLS,
                scopes=SCOPES,
                expires=None,
                task_machine=harness.executor_id,
            )
        }
        assert "read" in names
        assert "bash" not in names
        result = await invoke(harness, repo, actor, call)
        assert await invoke(harness, repo, actor, call) == result
        intent = repo.reserve(
            actor,
            call,
            machine=harness.executor_id,
            scopes=SCOPES,
            expires=None,
            registered={harness.executor_id},
        )
        assert intent.native_id == result["task_id"]
        other = DurableTaskRepository(
            original.database,
            scope=replace(activation.scope, activation_id=str(uuid4())),
        )
        with pytest.raises(PermissionError, match="does not belong"):
            other.bind(actor, intent, result["task_id"])
        created[activation.scope.member_id] = result["task_id"]
        if activation.scope.member_id == member["id"]:
            agents.update(
                lease.owner,
                member["id"],
                {"machine_default_permission": "none"},
            )
            with pytest.raises(PermissionError):
                await invoke(harness, repo, actor, call)
            revoked = original.cancellation_batch({harness.executor_id})
            assert {row["native_id"] for row in revoked} == {result["task_id"]}
        return GraphMemberResult(
            [*activation.history, {"role": "assistant", "content": "done"}]
        )

    await run_multi_agent_graph(
        actor,
        root["id"],
        [{"role": "user", "content": "graph"}],
        execute_member=execute,
        check_grant=lambda: None,
        recursion_limit=30,
    )
    assert set(created) == {root["id"], member["id"]}
    assert len(set(created.values())) == 2
    with original.database.connect() as db:
        rows = db.execute(
            "SELECT * FROM agent_durable_tasks WHERE run_id=?", (actor.run_id,)
        ).fetchall()
    assert len(rows) == 2
    assert all(row["call_id"].startswith("graph:") for row in rows)
    assert len({row["member_namespace"] for row in rows}) == 2


@pytest.mark.asyncio
async def test_graph_cached_native_receipt_cannot_cross_member_revocation(
    bound,
):
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.graph_runtime import (
        GraphMemberResult,
        run_multi_agent_graph,
    )
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig

    pytest.importorskip("langgraph")
    harness, original, lease, _ = bound
    agents = AgentRepository(original.database)
    member = agents.create(
        lease.owner, {"provider": "openai", "model": "member"}
    )
    root = agents.create(lease.owner, {"provider": "openai", "model": "root"})
    agents.update(
        lease.owner,
        root["id"],
        {
            "edges": [
                {"from": root["id"], "to": member["id"], "edgeType": "direct"}
            ],
        },
    )
    generation, _ = GenerationStore(original.database).begin(
        lease.owner,
        {"text": "graph", "agent_id": root["id"]},
    )
    actor = ActorLease.claim(original.database, lease.owner, generation.run_id)
    assert actor is not None

    async def execute(activation):
        if activation.scope.member_id == member["id"]:
            repo = DurableTaskRepository(
                original.database, scope=activation.scope
            )
            checkpoint = OwnedLoopCheckpoint(actor, scope=activation.scope)
            state = AgentLoopState(
                AgentContext(
                    ProviderConfig(
                        "openai", "model", "https://example.invalid", "fake"
                    ),
                    [],
                    agent=member,
                ),
                [],
            )
            node = NativeLoopToolNode(
                harness.control,
                repo,
                checkpoint,
                allowed_tools={"task"},
                scopes=SCOPES,
                expires=None,
                task_machine=harness.executor_id,
                check_grant=lambda: None,
            )
            await node.refresh_tools(state)
            calls = node.preflight(
                [ModelToolCall("same", "task", {"action": "create"})], state
            )
            state.pending_calls = calls
            state.tool_step_started = True
            await checkpoint.save(state, calls)
            result = await node.dispatch(calls, state)
            assert (
                await node.dispatch(calls, state)
            ).messages == result.messages
            agents.update(
                lease.owner,
                member["id"],
                {"machine_default_permission": "none"},
            )
            with pytest.raises(
                PermissionError, match="disclosure grant was reduced"
            ):
                await node.dispatch(calls, state)
            assert len(await harness.control.task_service.list_tasks()) == 1
        return GraphMemberResult(
            [*activation.history, {"role": "assistant", "content": "done"}]
        )

    await run_multi_agent_graph(
        actor,
        root["id"],
        [{"role": "user", "content": "graph"}],
        execute_member=execute,
        check_grant=lambda: None,
        recursion_limit=30,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "edge_type", ["direct", "handoff", "reused-id", "retry"]
)
async def test_native_graph_member_model_loop_and_completed_recovery(
    bound, edge_type
):
    from workgate.control.hosted_chat_graph_loop import run_native_graph
    from workgate.hosted_chat.agent_runtime import AgentContext
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.provider_common import ProviderConfig

    pytest.importorskip("langgraph")
    harness, repo, lease, _ = bound
    agents = AgentRepository(repo.database)
    member = agents.create(
        lease.owner,
        {
            "provider": "openai",
            "model": "member",
            "instructions": "member only",
        },
    )
    root = agents.create(
        lease.owner,
        {"provider": "openai", "model": "root", "instructions": "root only"},
    )
    agents.update(
        lease.owner,
        root["id"],
        {
            "edges": [
                {
                    "from": root["id"],
                    "to": member["id"],
                    "edgeType": "direct"
                    if edge_type in {"direct", "retry"}
                    else "handoff",
                }
            ]
        },
    )
    generation, _ = GenerationStore(repo.database).begin(
        lease.owner, {"text": "graph", "agent_id": root["id"]}
    )
    actor = ActorLease.claim(repo.database, lease.owner, generation.run_id)
    assert actor is not None
    turns = []
    authorized = []
    transfer_name = "transfer_to_" + member["id"]

    async def resolve(agent):
        return AgentContext(
            ProviderConfig(
                "openai", agent["model"], "https://example.invalid", "fake"
            ),
            [{"function": {"name": "forbidden"}}],
        )

    async def authorize(calls, state):
        authorized.append(
            (state.context.profile.model, [call.name for call in calls])
        )

    async def stream(profile, messages, **kwargs):
        name = profile.model
        turns.append(name)
        assert "forbidden" not in {
            tool["function"]["name"] for tool in kwargs["tools"]
        }
        assert [m["content"] for m in messages if m["role"] == "system"] == [
            name + " only"
        ]
        if turns.count(name) == 1:
            if name == "member" and edge_type == "reused-id":
                assert any(
                    m.get("role") == "tool"
                    and m.get("tool_call_id") == "same-id"
                    and "task_id" in m.get("content", "")
                    for m in messages
                )
                assert all(m.get("name") != transfer_name for m in messages)
            yield ModelToolCall("same-id", "task", {"action": "create"})
            if name == "root" and edge_type == "handoff":
                assert transfer_name in {
                    tool["function"]["name"] for tool in kwargs["tools"]
                }
                yield ModelToolCall("transfer-id", transfer_name, {})
        else:
            assert messages[-1]["role"] == "tool"
            if name == "root" and edge_type == "reused-id":
                yield ModelToolCall("same-id", transfer_name, {})
            elif (
                edge_type == "retry"
                and name == "member"
                and turns.count(name) == 2
            ):
                raise RuntimeError("simulated member interruption")
            else:
                yield name + " complete"

    async def execute(actor):
        return await run_native_graph(
            harness.control,
            actor,
            root["id"],
            [{"role": "user", "content": "graph"}],
            resolve_context=resolve,
            authorize_batch=authorize,
            allowed_tools={"task"},
            scopes=SCOPES,
            expires=None,
            task_machine=harness.executor_id,
            check_grant=lambda: None,
            recursion_limit=20,
            stream_tools=stream,
            stream_text=stream,
        )

    if edge_type == "retry":
        with pytest.raises(RuntimeError, match="simulated member interruption"):
            await execute(actor)
        assert len(await harness.control.task_service.list_tasks()) == 2
        actor.release()
        actor = ActorLease.claim(repo.database, lease.owner, generation.run_id)
        assert actor is not None
    result = await execute(actor)
    assert result[-1]["content"] == "member complete"
    assert len(await harness.control.task_service.list_tasks()) == 2
    assert turns.count("member") == (
        3 if edge_type == "retry" else 2
    ) and turns.count("root") == (1 if edge_type == "handoff" else 2)
    assert sum("task" in names for _, names in authorized) == 2
    if edge_type in {"handoff", "reused-id"}:
        assert len([m for m in result if m.get("name") == transfer_name]) == 1
    # The graph driver has no public-generation terminal authority.
    current = GenerationStore(repo.database).get(lease.owner, generation.run_id)
    assert current is not None and current.status == "running"
    before = turns.copy()
    actor.release()
    fresh = ActorLease.claim(repo.database, lease.owner, generation.run_id)
    assert fresh is not None
    assert await execute(fresh) == result
    assert turns == before
    assert len(await harness.control.task_service.list_tasks()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "recovery", ["normal", "prepublish-crash", "lost-graph", "revoked-root"]
)
async def test_native_graph_parallel_approval_preserves_completed_siblings(
    bound,
    monkeypatch,
    recovery,
):
    from workgate.control.hosted_chat_graph_loop import run_native_graph
    from workgate.hosted_chat.agent_runtime import AgentContext, AgentSuspended
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.approval_claims import ApprovalClaimStore
    from workgate.hosted_chat.provider_common import ProviderConfig

    pytest.importorskip("langgraph")
    harness, repo, lease, _ = bound
    agents = AgentRepository(repo.database)
    root = agents.create(lease.owner, {"provider": "openai", "model": "root"})
    members = {
        name: agents.create(
            lease.owner,
            {
                "provider": "openai",
                "model": name,
                "approval_mode": "automatic" if name == "completed" else "all",
            },
        )
        for name in ("left", "right", "completed")
    }
    agents.update(
        lease.owner,
        root["id"],
        {
            "edges": [
                {
                    "from": root["id"],
                    "to": [a["id"] for a in members.values()],
                    "edgeType": "direct",
                }
            ]
        },
    )
    generation, _ = GenerationStore(repo.database).begin(
        lease.owner, {"text": "graph", "agent_id": root["id"]}
    )
    store = ApprovalClaimStore(repo.database)
    turns = []

    async def resolve(agent):
        return AgentContext(
            ProviderConfig(
                "openai", agent["model"], "https://example.invalid", "fake"
            ),
            [],
        )

    async def authorize(calls, state):
        pass

    async def stream(profile, messages, **kwargs):
        name = profile.model
        turns.append(name)
        if name == "root" or turns.count(name) > 1:
            yield name + " complete"
        else:
            yield ModelToolCall(
                "same", "task", {"action": "create", "label": name}
            )

    async def execute():
        actor = ActorLease.claim(repo.database, lease.owner, generation.run_id)
        assert actor is not None
        try:
            return await run_native_graph(
                harness.control,
                actor,
                root["id"],
                [{"role": "user", "content": "graph"}],
                resolve_context=resolve,
                authorize_batch=authorize,
                allowed_tools={"task"},
                scopes=SCOPES,
                expires=None,
                task_machine=harness.executor_id,
                check_grant=lambda: None,
                recursion_limit=30,
                stream_tools=stream,
                stream_text=stream,
            )
        finally:
            actor.release()

    if recovery == "prepublish-crash":

        def crash(*args, **kwargs):
            raise RuntimeError(
                "simulated crash before publishing graph interrupt"
            )

        with monkeypatch.context() as patch:
            patch.setattr(ApprovalClaimStore, "publish_graph_interrupt", crash)
            with pytest.raises(RuntimeError, match="simulated crash"):
                await execute()
        assert store.pending(lease.owner, generation.run_id) is None
        assert len(await harness.control.task_service.list_tasks()) == 1

    namespaces = set()
    for expected in (1, 2):
        with pytest.raises(AgentSuspended, match="graph member"):
            await execute()
        pending = store.pending(lease.owner, generation.run_id)
        assert pending is not None
        assert pending["namespace"].startswith(
            generation.run_id + ":graph:foreground:member:"
        )
        assert pending["namespace"] not in namespaces
        namespaces.add(pending["namespace"])
        assert len(await harness.control.task_service.list_tasks()) == expected
        assert store.pending("other", generation.run_id) is None
        with pytest.raises(KeyError):
            store.claim(
                "other",
                generation.run_id,
                pending["actionId"],
                [{"tool_call_id": "same", "decision": "approve"}],
            )
        if recovery in {"lost-graph", "revoked-root"}:
            if recovery == "lost-graph":
                with repo.database.connect() as db:
                    db.execute(
                        "UPDATE agent_checkpoints SET expires_at=0 WHERE checkpoint_ns=?",
                        (generation.run_id + ":graph:foreground",),
                    )
            else:
                agents.update(
                    lease.owner, root["id"], {"name": "changed while parked"}
                )
            with pytest.raises((ValueError, PermissionError)):
                store.claim(
                    lease.owner,
                    generation.run_id,
                    pending["actionId"],
                    [{"tool_call_id": "same", "decision": "approve"}],
                )
            assert store.expire_pending(
                lease.owner, generation.run_id, pending["actionId"]
            )
            current = GenerationStore(repo.database).get(
                lease.owner, generation.run_id
            )
            assert current is not None and current.status == "interrupted"
            assert len(await harness.control.task_service.list_tasks()) == 1
            return
        store.claim(
            lease.owner,
            generation.run_id,
            pending["actionId"],
            [{"tool_call_id": "same", "decision": "approve"}],
        )
    result = await execute()
    assert {
        m["content"]
        for m in result
        if m.get("role") == "assistant" and m.get("content")
    } == {
        "root complete",
        "left complete",
        "right complete",
        "completed complete",
    }
    assert len(await harness.control.task_service.list_tasks()) == 3
    assert turns.count("root") == 1
    assert all(turns.count(name) == 2 for name in members)
    assert await execute() == result


@pytest.mark.asyncio
async def test_native_graph_approval_then_ask_user_same_activation(bound):
    from workgate.control.hosted_chat_graph_loop import run_native_graph
    from workgate.hosted_chat.agent_runtime import AgentContext, AgentSuspended
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.approval_claims import ApprovalClaimStore
    from workgate.hosted_chat.provider_common import ProviderConfig

    pytest.importorskip("langgraph")
    harness, repo, lease, _ = bound
    agents = AgentRepository(repo.database)
    root = agents.create(
        lease.owner,
        {"provider": "openai", "model": "root", "tools": ["ask_user_question"]},
    )
    member = agents.create(
        lease.owner,
        {
            "provider": "openai",
            "model": "member",
            "tools": ["ask_user_question"],
        },
    )
    agents.update(
        lease.owner,
        root["id"],
        {
            "edges": [
                {"from": root["id"], "to": member["id"], "edgeType": "direct"}
            ]
        },
    )
    WorkspaceRepository(repo.database).save_setting(
        lease.owner,
        "endpoints",
        {
            "agents": {
                "toolApproval": {"enabled": True, "ask": ["ask_user_question"]}
            }
        },
    )
    generation, _ = GenerationStore(repo.database).begin(
        lease.owner, {"text": "graph", "agent_id": root["id"]}
    )
    store = ApprovalClaimStore(repo.database)
    turns = []

    async def resolve(agent):
        return AgentContext(
            ProviderConfig(
                "openai", agent["model"], "https://example.invalid", "fake"
            ),
            [],
        )

    async def authorize(calls, state):
        pass

    async def stream(profile, messages, **kwargs):
        turns.append(profile.model)
        if profile.model == "root":
            yield "root complete"
        elif turns.count("member") == 1:
            yield ModelToolCall(
                "q", "ask_user_question", {"question": "Original?"}
            )
        else:
            assert json.loads(messages[-1]["content"]) == {"answer": "answer"}
            yield "member complete"

    async def execute():
        actor = ActorLease.claim(repo.database, lease.owner, generation.run_id)
        assert actor is not None
        try:
            return await run_native_graph(
                harness.control,
                actor,
                root["id"],
                [{"role": "user", "content": "graph"}],
                resolve_context=resolve,
                authorize_batch=authorize,
                allowed_tools={"ask_user_question"},
                scopes=set(),
                expires=None,
                task_machine=None,
                check_grant=lambda: None,
                recursion_limit=20,
                stream_tools=stream,
                stream_text=stream,
            )
        finally:
            actor.release()

    with pytest.raises(AgentSuspended):
        await execute()
    pending = store.pending(lease.owner, generation.run_id)
    assert pending is not None and pending["payload"]["type"] == "tool_approval"
    store.claim(
        lease.owner,
        generation.run_id,
        pending["actionId"],
        [
            {
                "tool_call_id": "q",
                "decision": "edit",
                "editedArguments": {"question": "Edited?"},
            }
        ],
    )
    with pytest.raises(AgentSuspended):
        await execute()
    question = store.pending(lease.owner, generation.run_id)
    assert question is not None
    assert question["namespace"] == pending["namespace"]
    assert question["payload"]["question"]["question"] == "Edited?"
    assert question["actionId"] != pending["actionId"]
    with pytest.raises(ValueError, match="Stale"):
        store.claim_question(
            lease.owner,
            generation.run_id,
            pending["actionId"],
            {"answer": "stale"},
        )
    store.claim_question(
        lease.owner,
        generation.run_id,
        question["actionId"],
        {"answer": "answer"},
    )
    result = await execute()
    assert result[-1]["content"] == "member complete"
    assert turns == ["root", "member", "member"]
    assert await execute() == result


@pytest.mark.asyncio
@pytest.mark.parametrize("crash", [False, True])
async def test_native_graph_steering_elects_one_recipient_and_recovers(
    bound, monkeypatch, crash
):
    from workgate.control.hosted_chat_graph_loop import run_native_graph
    from workgate.hosted_chat.agent_runtime import AgentContext
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.provider_common import ProviderConfig
    from workgate.hosted_chat.steering import SteeringStore

    pytest.importorskip("langgraph")
    harness, repo, lease, _ = bound
    agents = AgentRepository(repo.database)
    root = agents.create(lease.owner, {"provider": "openai", "model": "root"})
    members = [
        agents.create(lease.owner, {"provider": "openai", "model": name})
        for name in ("left", "right")
    ]
    agents.update(
        lease.owner,
        root["id"],
        {
            "edges": [
                {
                    "from": root["id"],
                    "to": [a["id"] for a in members],
                    "edgeType": "direct",
                }
            ]
        },
    )
    generation, _ = GenerationStore(repo.database).begin(
        lease.owner, {"text": "graph", "agent_id": root["id"]}
    )
    steering = SteeringStore(repo.database)
    submitted = []
    delivered = []
    turns = []
    arrived = set()
    both = asyncio.Event()
    drain = SteeringStore.drain
    failed = False

    def crash_after_drain(self, actor, *, boundary, scope=None):
        nonlocal failed
        applied = drain(self, actor, boundary=boundary, scope=scope)
        if crash and applied and not failed:
            failed = True
            raise RuntimeError("simulated crash after graph steer delivery")
        return applied

    monkeypatch.setattr(SteeringStore, "drain", crash_after_drain)

    async def resolve(agent):
        return AgentContext(
            ProviderConfig(
                "openai", agent["model"], "https://example.invalid", "fake"
            ),
            [],
        )

    async def authorize(calls, state):
        pytest.fail("Graph steer test must not execute tools")

    async def stream(profile, messages, **kwargs):
        name = profile.model
        turns.append(name)
        if name == "root":
            yield "root complete"
            return
        steers = [m["steerId"] for m in messages if m.get("source") == "steer"]
        if steers:
            delivered.append((name, steers))
            assert steers == submitted
            yield name + " redirected"
            return
        arrived.add(name)
        if len(arrived) == 2:
            if not submitted:
                for index in (1, 2):
                    ack = steering.enqueue(
                        lease.owner,
                        {
                            "conversationId": generation.conversation_id,
                            "text": f"steer {index}",
                            "clientSteerId": f"graph-{index}",
                        },
                        preempt_capable=True,
                        validate_submission=lambda text: None,
                    )
                    submitted.append(ack["steerId"])
            both.set()
        await asyncio.wait_for(both.wait(), 2)
        yield name + " initial"

    async def execute():
        actor = ActorLease.claim(repo.database, lease.owner, generation.run_id)
        assert actor is not None
        try:
            result = await run_native_graph(
                harness.control,
                actor,
                root["id"],
                [{"role": "user", "content": "graph"}],
                resolve_context=resolve,
                authorize_batch=authorize,
                allowed_tools=set(),
                scopes=set(),
                expires=None,
                task_machine=None,
                check_grant=lambda: None,
                recursion_limit=20,
                stream_tools=stream,
                stream_text=stream,
            )
            assert steering.applied_parts(actor) == []
            return result
        finally:
            actor.release()

    if crash:
        with pytest.raises(RuntimeError, match="simulated crash"):
            await execute()
    result = await execute()
    assert len(delivered) == 1
    assert delivered[0][1] == submitted
    assert turns.count("root") == 1
    assert [
        m["steerId"] for m in result if m.get("source") == "steer"
    ] == submitted
    events = [
        event
        for _, event in GenerationStore(repo.database).events(
            lease.owner, generation.run_id
        )
    ]
    applied = [
        event["data"]
        for event in events
        if event.get("event") == "on_steer_applied"
    ]
    assert [item["steerId"] for item in applied] == submitted
    assert all(
        "namespace" not in item and "recipientNamespace" not in item
        for item in applied
    )
    with repo.database.read_snapshot() as db:
        state = json.loads(
            db.execute(
                "SELECT state FROM runs WHERE id=?", (generation.run_id,)
            ).fetchone()[0]
        )
    assert len(set(state["steerDelivery"].values())) == 1
    assert set(state["steerDelivery"]) == set(submitted)
    assert await execute() == result


@pytest.mark.asyncio
@pytest.mark.parametrize("preempt", [False, True])
async def test_native_graph_handoff_keeps_post_tool_steer_with_recipient(
    bound, preempt
):
    from workgate.control.hosted_chat_graph_loop import run_native_graph
    from workgate.hosted_chat.agent_runtime import AgentContext
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.provider_common import ProviderConfig
    from workgate.hosted_chat.steering import SteeringStore

    pytest.importorskip("langgraph")
    harness, repo, lease, _ = bound
    agents = AgentRepository(repo.database)
    root = agents.create(lease.owner, {"provider": "openai", "model": "root"})
    member = agents.create(
        lease.owner, {"provider": "openai", "model": "member"}
    )
    agents.update(
        lease.owner,
        root["id"],
        {
            "edges": [
                {"from": root["id"], "to": member["id"], "edgeType": "handoff"}
            ]
        },
    )
    generation, _ = GenerationStore(repo.database).begin(
        lease.owner, {"text": "graph", "agent_id": root["id"]}
    )
    actor = ActorLease.claim(repo.database, lease.owner, generation.run_id)
    assert actor is not None
    steering = SteeringStore(repo.database)
    submitted = []
    turns = []

    async def resolve(agent):
        return AgentContext(
            ProviderConfig(
                "openai", agent["model"], "https://example.invalid", "fake"
            ),
            [],
        )

    async def authorize(calls, state):
        assert not preempt
        assert [call.name for call in calls] == ["transfer_to_" + member["id"]]

    async def stream(profile, messages, **kwargs):
        turns.append(profile.model)
        if profile.model == "root":
            if submitted:
                assert preempt and messages[-1]["steerId"] == submitted[0]
                yield "root redirected"
                return
            ack = steering.enqueue(
                lease.owner,
                {
                    "conversationId": generation.conversation_id,
                    "text": "redirect",
                    "clientSteerId": "handoff-steer",
                    "preempt": preempt,
                },
                preempt_capable=True,
                validate_submission=lambda text: None,
            )
            submitted.append(ack["steerId"])
            yield ModelToolCall("transfer", "transfer_to_" + member["id"], {})
        else:
            assert messages[-1]["source"] == "steer"
            assert messages[-1]["steerId"] == submitted[0]
            assert all(
                m.get("name") != "transfer_to_" + member["id"] for m in messages
            )
            yield "member redirected"

    result = await run_native_graph(
        harness.control,
        actor,
        root["id"],
        [{"role": "user", "content": "graph"}],
        resolve_context=resolve,
        authorize_batch=authorize,
        allowed_tools=set(),
        scopes=set(),
        expires=None,
        task_machine=None,
        check_grant=lambda: None,
        recursion_limit=20,
        stream_tools=stream,
        stream_text=stream,
    )
    assert turns == (["root", "root"] if preempt else ["root", "member"])
    assert [
        m["steerId"] for m in result if m.get("source") == "steer"
    ] == submitted
    assert result[-1]["content"] == (
        "root redirected" if preempt else "member redirected"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", ["automatic", "all"])
async def test_child_native_node_replays_private_receipt_without_parent_approval(
    bound, approval
):
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig
    from workgate.hosted_chat.subagent_scope import SubagentScope
    from workgate.hosted_chat.subagents import (
        build_subagent_configs,
        invoke_subagent,
    )

    harness, original, lease, _ = bound
    agents = AgentRepository(original.database)
    child = agents.create(lease.owner, {"provider": "openai", "model": "child"})
    root = agents.create(
        lease.owner,
        {
            "provider": "openai",
            "model": "parent",
            "approval_mode": approval,
            "subagents": {"enabled": True, "agent_ids": [child["id"]]},
        },
    )
    generation, _ = GenerationStore(original.database).begin(
        lease.owner, {"text": "parent private", "agent_id": root["id"]}
    )
    actor = ActorLease.claim(original.database, lease.owner, generation.run_id)
    assert actor is not None
    parent = OwnedLoopCheckpoint(actor)
    provider = ProviderConfig(
        "openai", "model", "https://example.invalid", "fake"
    )
    delegate = ModelToolCall(
        "delegate",
        "subagent",
        {"subagent_type": child["id"], "description": "child task"},
    )
    parent_state = AgentLoopState(
        AgentContext(provider, [], agent=root),
        [],
        pending_calls=[delegate],
        tool_step_started=True,
    )
    await parent.save(parent_state, [delegate])
    configs = build_subagent_configs(lease.owner, root, original.database)
    captured = []
    live = set(SCOPES)

    async def execute(value):
        scope = SubagentScope.admit(parent, delegate, value)
        captured.append(scope)
        repo = DurableTaskRepository(original.database, scope=scope)
        checkpoint = OwnedLoopCheckpoint(actor, scope=scope)
        node = NativeLoopToolNode(
            harness.control,
            repo,
            checkpoint,
            allowed_tools={"task"},
            scopes=set(SCOPES),
            expires=None,
            task_machine=harness.executor_id,
            check_grant=lambda: None,
        )
        state = AgentLoopState(
            AgentContext(provider, [], agent=child), value.messages
        )
        await node.refresh_tools(state)
        calls = node.preflight(
            [ModelToolCall("same", "task", {"action": "create"})], state
        )
        state.pending_calls = calls
        state.tool_step_started = True
        await checkpoint.save(state, calls)
        result = await node.dispatch(calls, state)
        assert (await node.dispatch(calls, state)).messages == result.messages
        if approval == "all":
            assert "requires human approval" in str(result.messages)
            assert not await harness.control.task_service.list_tasks()
        else:
            assert len(await harness.control.task_service.list_tasks()) == 1
            # Conversation ACL changes do not alter the child revision; receipt
            # disclosure still has to check the live machine ceiling.
            WorkspaceRepository(original.database).save_setting(
                lease.owner,
                "agent.machine_policy.conversation."
                + generation.conversation_id,
                {harness.executor_id: "none"},
            )
            with pytest.raises(
                PermissionError, match="disclosure grant was reduced"
            ):
                await node.dispatch(calls, state)
        return "done"

    assert (
        await invoke_subagent(
            delegate,
            configs,
            execute_child=execute,
            current_grant=lambda: (live, None),
            owner=lease.owner,
            conversation_id=generation.conversation_id,
            parent_run_id=actor.run_id,
            database=original.database,
            parent_agent_id=root["id"],
        )
        == "done"
    )
    assert captured
    with original.database.read_snapshot() as db:
        row = db.execute(
            "SELECT state FROM runs WHERE id=?", (actor.run_id,)
        ).fetchone()
        assert "pendingApproval" not in json.loads(row["state"])


@pytest.mark.asyncio
@pytest.mark.parametrize("ordinary", [False, True])
async def test_foreground_subagent_model_loop_and_replay(bound, ordinary):
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.control.hosted_chat_subagent_loop import ForegroundDelegation
    from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import (
        ModelThought,
        ProviderConfig,
    )
    from workgate.hosted_chat.subagents import (
        build_subagent_configs,
        chat_subagent_config,
    )

    harness, original, lease, generation = bound
    agents = AgentRepository(original.database)
    child = agents.create(
        lease.owner,
        {
            "provider": "openai",
            "model": "child",
            "instructions": "child instructions",
        },
    )
    root = (
        None
        if ordinary
        else agents.create(
            lease.owner,
            {
                "provider": "openai",
                "model": "parent",
                "subagents": {"enabled": True, "agent_ids": [child["id"]]},
            },
        )
    )
    if root is not None:
        generation, _ = GenerationStore(original.database).begin(
            lease.owner,
            {"text": "parent private", "agent_id": root["id"]},
        )
        actor = ActorLease.claim(
            original.database, lease.owner, generation.run_id
        )
        assert actor is not None
    else:
        actor = lease
    context = AgentContext(
        ProviderConfig("openai", "parent", "https://example.invalid", "fake"),
        [],
        agent=root,
    )
    configs = (
        [chat_subagent_config(lease.owner)]
        if ordinary
        else build_subagent_configs(lease.owner, root, original.database)
    )
    models = []
    effects = []

    async def resolve(agent):
        assert not ordinary
        assert agent["id"] == child["id"]
        return AgentContext(
            ProviderConfig(
                "openai", "child", "https://example.invalid", "fake"
            ),
            [],
            agent=agent,
        )

    async def stream(profile, messages, *, tools, **_kwargs):
        assert profile.model == ("parent" if ordinary else "child")
        assert "parent private" not in str(messages)
        assert {"role": "user", "content": "child task"} in messages
        assert "subagent" not in {tool["function"]["name"] for tool in tools}
        models.append(profile.model)
        if not any(message["role"] == "tool" for message in messages):
            yield ModelThought("thinking", text="private reasoning")
            yield ModelToolCall("same", "task", {"action": "create"})
        else:
            yield "child final"

    async def authorize(calls, state):
        effects.extend(call.name for call in calls)

    delegation = ForegroundDelegation(configs, resolve, stream_tools=stream)

    def make_node(checkpoint):
        return NativeLoopToolNode(
            harness.control,
            original,
            checkpoint,
            allowed_tools={"task"},
            scopes=set(SCOPES),
            expires=None,
            task_machine=harness.executor_id,
            check_grant=lambda: None,
            delegation=delegation,
        )

    parent = OwnedLoopCheckpoint(actor)
    node = make_node(parent)
    state = AgentLoopState(
        context, [{"role": "user", "content": "parent private"}]
    )
    await node.refresh_tools(state)
    assert "subagent" in {tool["function"]["name"] for tool in context.tools}
    target = "self" if ordinary else child["id"]
    call = ModelToolCall(
        "delegate",
        "subagent",
        {"subagent_type": target, "description": "child task"},
    )
    calls = node.preflight([call], state)
    state.pending_calls = calls
    state.tool_step_started = True
    await parent.save(state, calls)
    # Simulate completion of the child just before parent receipt publication.
    result, _ = await delegation.invoke(node, call, context, authorize)
    assert result["content"] == [{"type": "text", "text": "child final"}]
    assert len(models) == 2
    actor.release()
    actor = ActorLease.claim(original.database, lease.owner, generation.run_id)
    assert actor is not None
    parent = OwnedLoopCheckpoint(actor)
    recovered = parent.restore(context)
    assert recovered is not None
    node = make_node(parent)
    output = await node.dispatch(
        calls, recovered.state, authorize_effects=authorize
    )
    assert output.messages[0]["content"] == "child final"
    assert "private reasoning" not in str(output)
    assert len(models) == 2
    assert len(await harness.control.task_service.list_tasks()) == 1
    assert effects == ["task", "subagent"]
    assert (
        await node.dispatch(calls, recovered.state)
    ).messages == output.messages
    if root is not None:
        agents.update(lease.owner, child["id"], {"instructions": "updated"})
        with pytest.raises(PermissionError, match="revision changed"):
            await node.dispatch(calls, recovered.state)


@pytest.mark.asyncio
async def test_foreground_subagent_recovery_rejects_changed_disclosure_ceiling(
    bound,
):
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.control.hosted_chat_subagent_loop import ForegroundDelegation
    from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig
    from workgate.hosted_chat.subagents import chat_subagent_config

    harness, original, actor, generation = bound
    parent = OwnedLoopCheckpoint(actor)
    context = AgentContext(
        ProviderConfig("openai", "parent", "https://example.invalid", "fake"),
        [],
    )

    async def unexpected(_agent):
        pytest.fail("ordinary child must inherit provider")

    async def stream(*_args, **_kwargs):
        yield "child final"

    delegation = ForegroundDelegation(
        [chat_subagent_config(actor.owner)], unexpected, stream_tools=stream
    )
    node = NativeLoopToolNode(
        harness.control,
        original,
        parent,
        allowed_tools={"task"},
        scopes=set(SCOPES),
        expires=None,
        task_machine=harness.executor_id,
        check_grant=lambda: None,
        delegation=delegation,
    )
    state = AgentLoopState(context, [])
    await node.refresh_tools(state)
    call = ModelToolCall(
        "child", "subagent", {"subagent_type": "self", "description": "task"}
    )
    state.pending_calls = node.preflight([call], state)
    state.tool_step_started = True
    await parent.save(state, state.pending_calls)
    await delegation.invoke(node, call, context, None)
    WorkspaceRepository(original.database).save_setting(
        actor.owner,
        "agent.machine_policy.conversation." + generation.conversation_id,
        {harness.executor_id: "none"},
    )
    with pytest.raises(PermissionError, match="changed after admission"):
        await node.dispatch([call], state)
    with pytest.raises(PermissionError, match="not an advertised"):
        delegation.validate(
            ModelToolCall(
                "other",
                "subagent",
                {"subagent_type": "self", "run_in_background": True},
            )
        )


@pytest.mark.asyncio
async def test_foreground_subagent_nested_result_is_fenced_by_leaf_revision(
    bound,
):
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.control.hosted_chat_subagent_loop import ForegroundDelegation
    from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig
    from workgate.hosted_chat.subagents import build_subagent_configs

    harness, original, lease, _ = bound
    agents = AgentRepository(original.database)
    leaf = agents.create(lease.owner, {"provider": "openai", "model": "leaf"})
    child = agents.create(
        lease.owner,
        {
            "provider": "openai",
            "model": "child",
            "subagents": {
                "enabled": True,
                "allowSelf": False,
                "agent_ids": [leaf["id"]],
            },
        },
    )
    root = agents.create(
        lease.owner,
        {
            "provider": "openai",
            "model": "parent",
            "subagents": {
                "enabled": True,
                "allowSelf": False,
                "agent_ids": [child["id"]],
            },
        },
    )
    generation, _ = GenerationStore(original.database).begin(
        lease.owner,
        {"text": "parent private", "agent_id": root["id"]},
    )
    actor = ActorLease.claim(original.database, lease.owner, generation.run_id)
    assert actor is not None
    parent = OwnedLoopCheckpoint(actor)

    async def resolve(agent):
        return AgentContext(
            ProviderConfig(
                "openai", agent["model"], "https://example.invalid", "fake"
            ),
            [],
            agent=agent,
        )

    async def stream(profile, messages, *, tools, **_kwargs):
        assert "parent private" not in str(messages)
        names = {tool["function"]["name"] for tool in tools}
        if profile.model == "leaf":
            assert "subagent" not in names
            assert messages == [{"role": "user", "content": "leaf task"}]
            yield "leaf result"
        else:
            assert profile.model == "child"
            assert "subagent" in names
            if any(message["role"] == "tool" for message in messages):
                assert "leaf result" in str(messages)
                yield "child result"
            else:
                yield ModelToolCall(
                    "same",
                    "subagent",
                    {"subagent_type": leaf["id"], "description": "leaf task"},
                )

    delegation = ForegroundDelegation(
        build_subagent_configs(lease.owner, root, original.database),
        resolve,
        stream_tools=stream,
    )
    node = NativeLoopToolNode(
        harness.control,
        original,
        parent,
        allowed_tools={"task"},
        scopes=set(SCOPES),
        expires=None,
        task_machine=harness.executor_id,
        check_grant=lambda: None,
        delegation=delegation,
    )
    context = await resolve(root)
    state = AgentLoopState(
        context, [{"role": "user", "content": "parent private"}]
    )
    await node.refresh_tools(state)
    call = ModelToolCall(
        "same",
        "subagent",
        {"subagent_type": child["id"], "description": "child task"},
    )
    state.pending_calls = node.preflight([call], state)
    state.tool_step_started = True
    await parent.save(state, state.pending_calls)
    output = await node.dispatch([call], state)
    assert output.messages[0]["content"] == "child result"
    assert "leaf result" not in str(output)
    assert (await node.dispatch([call], state)).messages == output.messages
    agents.update(lease.owner, leaf["id"], {"instructions": "revoked"})
    with pytest.raises(PermissionError, match="revision changed"):
        await node.dispatch([call], state)


@pytest.mark.asyncio
async def test_foreground_team_result_only_native_effects_and_recovery(bound):
    pytest.importorskip("langgraph")
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.control.hosted_chat_subagent_loop import ForegroundDelegation
    from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig
    from workgate.hosted_chat.subagents import build_subagent_configs

    harness, original, lease, _ = bound
    agents = AgentRepository(original.database)
    entry = agents.create(lease.owner, {"provider": "openai", "model": "entry"})
    final = agents.create(lease.owner, {"provider": "openai", "model": "final"})
    root = agents.create(
        lease.owner,
        {
            "provider": "openai",
            "model": "parent",
            "subagents": {
                "enabled": True,
                "allowSelf": False,
                "graphs": [
                    {
                        "type": "team",
                        "name": "Review team",
                        "description": "Review",
                        "agent_ids": [entry["id"], final["id"]],
                        "entry_agent_id": entry["id"],
                        "result_agent_id": final["id"],
                        "edges": [
                            {
                                "from": entry["id"],
                                "to": final["id"],
                                "edgeType": "direct",
                            }
                        ],
                    }
                ],
            },
        },
    )
    generation, _ = GenerationStore(original.database).begin(
        lease.owner,
        {"text": "parent private", "agent_id": root["id"]},
    )
    actor = ActorLease.claim(original.database, lease.owner, generation.run_id)
    assert actor is not None
    models = []

    async def resolve(agent):
        return AgentContext(
            ProviderConfig(
                "openai", agent["model"], "https://example.invalid", "fake"
            ),
            [],
            agent=agent,
        )

    async def stream(profile, messages, *, tools, **_kwargs):
        assert "parent private" not in str(messages)
        assert "subagent" not in {tool["function"]["name"] for tool in tools}
        models.append(profile.model)
        if profile.model == "entry":
            if not any(m["role"] == "tool" for m in messages):
                yield ModelToolCall("same", "task", {"action": "create"})
            else:
                yield "intermediate team evidence"
        else:
            assert profile.model == "final"
            assert "intermediate team evidence" in str(messages)
            if not any(
                m.get("role") == "tool"
                and m.get("tool_call_id") == "final-call"
                for m in messages
            ):
                yield ModelToolCall("final-call", "task", {"action": "create"})
            else:
                yield "designated team result"

    delegation = ForegroundDelegation(
        build_subagent_configs(lease.owner, root, original.database),
        resolve,
        stream_tools=stream,
    )

    def make_node(checkpoint):
        return NativeLoopToolNode(
            harness.control,
            original,
            checkpoint,
            allowed_tools={"task"},
            scopes=set(SCOPES),
            expires=None,
            task_machine=harness.executor_id,
            check_grant=lambda: None,
            delegation=delegation,
        )

    parent = OwnedLoopCheckpoint(actor)
    node = make_node(parent)
    context = await resolve(root)
    state = AgentLoopState(
        context, [{"role": "user", "content": "parent private"}]
    )
    await node.refresh_tools(state)
    call = ModelToolCall(
        "team-call",
        "subagent",
        {"subagent_type": "team", "description": "review"},
    )
    state.pending_calls = node.preflight([call], state)
    state.tool_step_started = True
    await parent.save(state, state.pending_calls)
    result, _ = await delegation.invoke(node, call, context, None)
    assert result["content"] == [
        {"type": "text", "text": "designated team result"}
    ]
    assert models == ["entry", "entry", "final", "final"]
    actor.release()
    actor = ActorLease.claim(original.database, lease.owner, generation.run_id)
    assert actor is not None
    parent = OwnedLoopCheckpoint(actor)
    recovered = parent.restore(context)
    assert recovered is not None
    node = make_node(parent)
    output = await node.dispatch([call], recovered.state)
    assert output.messages[0]["content"] == "designated team result"
    assert len(models) == 4
    assert len(await harness.control.task_service.list_tasks()) == 2
    with original.database.read_snapshot() as db:
        rows = db.execute(
            "SELECT member_namespace,member_agent_id FROM agent_durable_tasks WHERE run_id=?",
            (actor.run_id,),
        ).fetchall()
        assert len(rows) == 2
        assert {row["member_agent_id"] for row in rows} == {
            entry["id"],
            final["id"],
        }
        assert all(
            ":graph:team:member:" in row["member_namespace"] for row in rows
        )
    assert (
        await node.dispatch([call], recovered.state)
    ).messages == output.messages
    assert original.cancellation_batch((harness.executor_id,)) == []
    agents.update(lease.owner, final["id"], {"instructions": "changed"})
    with pytest.raises(PermissionError, match="target revision changed"):
        await node.dispatch([call], recovered.state)
    with original.database.connect() as db:
        db.execute(
            "UPDATE agent_durable_tasks SET next_attempt_at=NULL WHERE run_id=?",
            (actor.run_id,),
        )
    assert len(original.cancellation_batch((harness.executor_id,))) == 2


def test_foreground_team_definition_requires_optional_graph_support(
    monkeypatch,
):
    from workgate.control import hosted_chat_subagent_loop as module
    from workgate.hosted_chat.subagents import (
        SubagentConfig,
        chat_subagent_config,
    )

    team = SubagentConfig(
        "team",
        "Team",
        "Review",
        {"id": "entry", "graphAgentIds": ["entry", "result"]},
        5,
        "alice",
    )
    ordinary = chat_subagent_config("alice")

    async def resolve(_agent):
        pytest.fail("advertisement must not resolve a provider")

    monkeypatch.setattr(module, "find_spec", lambda _: None)
    delegation = module.ForegroundDelegation([team, ordinary], resolve)
    assert [item.type for item in delegation.configs] == ["self"]
    with pytest.raises(PermissionError, match="advertised"):
        delegation.validate(
            ModelToolCall("id", "subagent", {"subagent_type": "team"})
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interruption",
    ["shutdown", "cancel", "revoke", "timeout", "interrupt", "none"],
)
async def test_detached_native_worker_recovery_and_cancellation(
    bound, monkeypatch, interruption
):
    import asyncio

    from workgate.control import hosted_chat_detached_worker as worker
    from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.detached_subagents import reserve_detached_child
    from workgate.hosted_chat.detached_task_controls import check_detached_task
    from workgate.hosted_chat.detached_task_lease import request_cancel
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig
    from workgate.hosted_chat.subagents import (
        build_subagent_configs,
        invoke_subagent,
    )

    harness, original, lease, _ = bound
    db = original.database
    agents = AgentRepository(db)
    child = agents.create(lease.owner, {"provider": "openai", "model": "child"})
    root = agents.create(
        lease.owner,
        {
            "provider": "openai",
            "model": "parent",
            "subagents": {
                "enabled": True,
                "allowSelf": False,
                "agent_ids": [child["id"]],
            },
        },
    )
    generation, _ = GenerationStore(db).begin(
        lease.owner, {"text": "parent private", "agent_id": root["id"]}
    )
    actor = ActorLease.claim(db, lease.owner, generation.run_id)
    assert actor is not None
    checkpoint = OwnedLoopCheckpoint(actor)
    call = ModelToolCall(
        "detached",
        "subagent",
        {
            "subagent_type": child["id"],
            "description": "child task",
            "run_in_background": True,
        },
    )
    parent_context = AgentContext(
        ProviderConfig("openai", "parent", "https://example.invalid", "fake"),
        [],
        agent=root,
    )
    state = AgentLoopState(
        parent_context,
        [{"role": "user", "content": "parent private"}],
        pending_calls=[call],
        tool_step_started=True,
    )
    await checkpoint.save(state, [call])
    access = {
        "scopes": sorted(SCOPES),
        "machines": {harness.executor_id: "write"},
        "task_machine": harness.executor_id,
        "allowed_tools": ["task"],
    }
    reservations = []

    async def reserve(value):
        item = reserve_detached_child(
            checkpoint, call, value, disclosure_access=access
        )
        reservations.append(item)
        return item.task_id

    async def not_foreground(_value):
        pytest.fail("detached call must not run foreground")

    task_id = await invoke_subagent(
        call,
        build_subagent_configs(lease.owner, root, db),
        execute_child=not_foreground,
        execute_background=reserve,
        current_grant=lambda: (set(SCOPES), None),
        owner=lease.owner,
        conversation_id=generation.conversation_id,
        parent_run_id=actor.run_id,
        database=db,
        parent_agent_id=root["id"],
    )
    assert task_id == reservations[0].task_id
    if interruption != "interrupt":
        assert GenerationStore(db).finish(
            lease.owner,
            actor.run_id,
            status="complete",
            text="accepted",
            actor_lease=actor,
        )
        actor.release()
    blocked = asyncio.Event()
    resume = asyncio.Event()
    turns = []
    control_counts = []
    authorized = []
    pause = interruption != "none"
    revoked = False

    def check_grant():
        if revoked:
            raise PermissionError("grant revoked")

    async def resolve(child_generation, agent):
        assert agent == child
        return AgentContext(
            ProviderConfig(
                "openai",
                child_generation.model,
                "https://example.invalid",
                "fake",
            ),
            [],
            agent=agent,
        )

    async def stream(profile, messages, *, tools, **_kwargs):
        assert profile.model == "child"
        assert "parent private" not in str(messages)
        assert {tool["function"]["name"] for tool in tools} == {"task"}
        turns.append(len(messages))
        control_counts.append(
            sum(m.get("content") == "change direction" for m in messages)
        )
        if not any(m["role"] == "tool" for m in messages):
            yield ModelToolCall("native-task", "task", {"action": "create"})
        else:
            if pause:
                blocked.set()
                await resume.wait()
            if (
                interruption == "interrupt"
                and not pause
                and any(
                    m.get("content") == "change direction" for m in messages
                )
            ):
                assert (
                    sum(
                        m.get("content") == "change direction" for m in messages
                    )
                    == 1
                )
            yield "detached final"

    async def authorize(calls, _state):
        authorized.extend(call.call_id for call in calls)

    async def execute():
        return await worker.run_detached_subagent(
            harness.control,
            db,
            lease.owner,
            generation.conversation_id,
            task_id,
            scopes=set(SCOPES),
            expires=None,
            disclosure_access=access,
            allowed_tools={"task", "bash"},
            check_grant=check_grant,
            resolve_context=resolve,
            authorize_batch=authorize,
            stream_tools=stream,
        )

    if interruption == "none":
        assert await execute()
    else:
        monkeypatch.setattr(worker, "_RENEW_SECONDS", 0.01)
        if interruption == "timeout":
            monkeypatch.setattr(worker, "_TIMEOUT_SECONDS", 2)
        running = asyncio.create_task(execute())
        await asyncio.wait_for(blocked.wait(), timeout=5)
        assert len(await harness.control.task_service.list_tasks()) == 1
        if interruption == "shutdown":
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            pause = False
            assert await execute()
        elif interruption == "interrupt":
            interrupt = ModelToolCall(
                "interrupt",
                "check_background_task",
                {
                    "background_task_id": task_id,
                    "action": "interrupt",
                    "message": "change direction",
                },
            )
            parent_state = AgentLoopState(
                parent_context,
                [],
                step=1,
                pending_calls=[interrupt],
                tool_step_started=True,
            )
            await checkpoint.save(parent_state, [interrupt])
            receipt = check_detached_task(
                checkpoint,
                interrupt,
                scopes=set(SCOPES),
                expires=None,
                disclosure_access=access,
            )
            assert receipt["status"] == "accepted"
            pause = False
            resume.set()
            assert await asyncio.wait_for(running, timeout=5)
            with db.read_snapshot() as conn:
                row = conn.execute(
                    "SELECT status,boundary FROM subagent_task_controls WHERE id=?",
                    (receipt["control_id"],),
                ).fetchone()
                assert tuple(row) == ("applied", "preempt")
            assert control_counts[-1] == 1
            actor.release()
        else:
            if interruption == "cancel":
                request_cancel(
                    db, lease.owner, generation.conversation_id, task_id
                )
            elif interruption == "revoke":
                revoked = True
            assert not await asyncio.wait_for(running, timeout=5)
            revoked = False

    assert len(await harness.control.task_service.list_tasks()) == 1
    assert authorized == ["native-task"]
    with db.read_snapshot() as conn:
        row = conn.execute(
            "SELECT status,result,child_run_id,lease_owner FROM subagent_tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        assert row["lease_owner"] is None
        child_run = row["child_run_id"]
        if interruption == "cancel":
            assert row["status"] == "cancelled" and row["result"] is None
        elif interruption in {"revoke", "timeout"}:
            assert row["status"] == "error" and row["result"] is None
        else:
            assert (
                row["status"] == "completed"
                and row["result"] == "detached final"
            )
        assert (
            conn.execute(
                "SELECT count(*) FROM agent_durable_tasks WHERE run_id=?",
                (child_run,),
            ).fetchone()[0]
            == 1
        )
        events = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT data FROM generation_events WHERE run_id=?",
                (child_run,),
            )
        ]
        assert len(events) == 1 and events[0]["final"]
    assert not await execute()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["complete", "recover", "revoke"])
async def test_authenticated_wakeup_reuses_foreground_loop(
    bound, monkeypatch, outcome
):
    import asyncio

    from workgate.control import hosted_chat_wakeup_worker as worker
    from workgate.control.hosted_chat_native_tool_node import NativeLoopToolNode
    from workgate.hosted_chat.agent_runtime import AgentContext, AgentLoopState
    from workgate.hosted_chat.agents import AgentRepository
    from workgate.hosted_chat.detached_subagents import reserve_detached_child
    from workgate.hosted_chat.detached_task_lease import DetachedTaskLease
    from workgate.hosted_chat.loop_checkpoint import OwnedLoopCheckpoint
    from workgate.hosted_chat.provider_common import ProviderConfig
    from workgate.hosted_chat.storage import WorkspaceRepository
    from workgate.hosted_chat.subagents import (
        build_subagent_configs,
        invoke_subagent,
    )

    harness, original, lease, _ = bound
    db = original.database
    agents = AgentRepository(db)
    child = agents.create(lease.owner, {"provider": "openai", "model": "child"})
    parent = agents.create(
        lease.owner,
        {
            "provider": "openai",
            "model": "parent",
            "subagents": {
                "enabled": True,
                "allowSelf": False,
                "agent_ids": [child["id"]],
            },
        },
    )
    store = GenerationStore(db)
    generation, _ = store.begin(
        lease.owner, {"text": "parent question", "agent_id": parent["id"]}
    )
    actor = ActorLease.claim(db, lease.owner, generation.run_id)
    assert actor is not None
    checkpoint = OwnedLoopCheckpoint(actor)
    node = NativeLoopToolNode(
        harness.control,
        original,
        checkpoint,
        allowed_tools={"task"},
        scopes=set(SCOPES),
        expires=None,
        task_machine=harness.executor_id,
        check_grant=lambda: None,
    )
    access = node._receipt_access()
    access["allowed_tools"] = ["task"]
    call = ModelToolCall(
        "detached",
        "subagent",
        {
            "subagent_type": child["id"],
            "description": "child task",
            "run_in_background": True,
        },
    )
    ctx = AgentContext(
        ProviderConfig("openai", "parent", "https://example.invalid", "fake"),
        [],
        agent=parent,
    )
    await checkpoint.save(
        AgentLoopState(
            ctx,
            [{"role": "user", "content": "parent question"}],
            pending_calls=[call],
            tool_step_started=True,
        ),
        [call],
    )
    reserved = []

    async def reserve(value):
        item = reserve_detached_child(
            checkpoint, call, value, disclosure_access=access
        )
        reserved.append((item, value))
        return item.task_id

    async def not_foreground(_value):
        pytest.fail("unexpected foreground delegation")

    task_id = await invoke_subagent(
        call,
        build_subagent_configs(lease.owner, parent, db),
        execute_child=not_foreground,
        execute_background=reserve,
        current_grant=lambda: (set(SCOPES), None),
        owner=lease.owner,
        conversation_id=generation.conversation_id,
        parent_run_id=actor.run_id,
        database=db,
        parent_agent_id=parent["id"],
    )
    item, value = reserved[0]
    assert item.task_id == task_id
    child_lease = DetachedTaskLease.claim(
        db,
        lease.owner,
        generation.conversation_id,
        task_id,
        scopes=set(SCOPES),
        expires=None,
        disclosure_access=access,
    )
    assert child_lease is not None
    child_lease.materialize(
        scopes=set(SCOPES), expires=None, disclosure_access=access
    )
    child_lease.complete(
        value.messages
        + [{"role": "assistant", "content": "private final answer"}],
        scopes=set(SCOPES),
        expires=None,
        disclosure_access=access,
    )
    store.finish(
        lease.owner,
        actor.run_id,
        status="complete",
        text="parent reply",
        actor_lease=actor,
    )
    actor.release()
    paused = asyncio.Event()
    resume = asyncio.Event()
    waiting = outcome != "complete"
    authorized = []

    async def resolve(wakeup):
        assert wakeup.agent_id == parent["id"]
        return ctx

    async def stream(profile, messages, *, tools, **kwargs):
        assert profile.model == "parent"
        assert messages[0]["content"] == "parent question"
        assert messages[1]["content"] == "parent reply"
        assert "private final answer" in messages[2]["content"]
        assert {tool["function"]["name"] for tool in tools} == {"task"}
        if not any(m["role"] == "tool" for m in messages):
            yield ModelToolCall("native-task", "task", {"action": "create"})
        else:
            if waiting:
                paused.set()
                await resume.wait()
            yield "continued answer"

    async def authorize(calls, _state):
        authorized.extend(call.call_id for call in calls)

    async def execute():
        return await worker.run_authenticated_wakeup(
            harness.control,
            db,
            lease.owner,
            task_id,
            scopes=set(SCOPES),
            expires=None,
            disclosure_access=access,
            allowed_tools={"task", "bash"},
            check_grant=lambda: None,
            resolve_context=resolve,
            authorize_batch=authorize,
            stream_tools=stream,
        )

    if outcome == "complete":
        assert await execute()
    else:
        monkeypatch.setattr(worker, "_RENEW_SECONDS", 0.01)
        running = asyncio.create_task(execute())
        await asyncio.wait_for(paused.wait(), timeout=5)
        if outcome == "recover":
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            waiting = False
            assert await execute()
        else:
            WorkspaceRepository(db).save_setting(
                lease.owner,
                "agent.machine_policy.conversation."
                + generation.conversation_id,
                {harness.executor_id: "none"},
            )
            resume.set()
            assert not await asyncio.wait_for(running, timeout=5)
    assert len(await harness.control.task_service.list_tasks()) == 1
    assert authorized == ["native-task"]
    with db.read_snapshot() as conn:
        row = conn.execute(
            "SELECT w.status,w.run_id,t.result_claimed FROM subagent_wakeups w JOIN subagent_tasks t ON t.id=w.task_id WHERE w.task_id=?",
            (task_id,),
        ).fetchone()
        if outcome == "revoke":
            assert row["status"] == "dead" and row["result_claimed"] == 0
        else:
            assert row["status"] == "done" and row["result_claimed"] == 1
            assert store.get(lease.owner, row["run_id"]) is not None
            reply = conn.execute(
                "SELECT text FROM messages WHERE conversation_id=? AND role='assistant' ORDER BY created_at DESC LIMIT 1",
                (generation.conversation_id,),
            ).fetchone()
            assert reply["text"] == "continued answer"
            assert not await execute()
