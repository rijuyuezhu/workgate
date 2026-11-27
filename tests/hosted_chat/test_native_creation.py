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
