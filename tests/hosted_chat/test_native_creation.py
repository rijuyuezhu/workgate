"""Paired native Task/Session creation with durable intent reconciliation."""

import asyncio
import time

import pytest

from tests.support.paired import build_paired_control_harness
from workgate.audit import query_audit
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.execution_context import control_execution_context
from workgate.control.executor_transport import ExecutorTransportError
from workgate.control.hosted_chat_cancellation import sweep_native_cancellations
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
