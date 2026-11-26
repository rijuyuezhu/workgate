"""Private reserved Job creation uses the native executor lifecycle."""

import asyncio

import pytest

from tests.support.paired import build_paired_control_harness
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.executor.jobs import shell as job_shell
from workgate.jobs.persistence import attempt_paths
from workgate.persistence import use_state_store
from workgate.protocol.executor import JOB_START_RESERVED_OP
from workgate.protocol.ids import new_reserved_job_id
from workgate.schemas.result_models.shell import StartPersistentShellOutput


@pytest.fixture
def bound(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    monkeypatch.setenv("WORKGATE_AUTH_MODE", "none")
    clear_settings_cache()
    harness = build_paired_control_harness(get_settings())
    started = []

    async def start(
        _config, _store, cwd, name, command, *, owner_session_id=None
    ):
        started.append(name)
        return StartPersistentShellOutput(
            shell_id=name, name=name, cwd=cwd, command=command, backend="fake"
        )

    async def active(*args):
        return set(started)

    monkeypatch.setattr(job_shell, "start_persistent_shell_execute", start)
    monkeypatch.setattr(
        job_shell, "authoritative_persistent_shell_ids_execute", active
    )
    yield harness, started
    clear_settings_cache()


async def session(harness):
    result = await harness.control.session_coordinator.start_session(
        executor_id=harness.executor_id
    )
    return result["session_id"]


async def call(harness, sid, payload):
    return await harness.call(
        harness.executor_id, JOB_START_RESERVED_OP, payload, session_id=sid
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,argument",
    [
        ("bash", {"command": "echo first"}),
        ("run_python_code", {"code": "print('first')"}),
    ],
)
async def test_reserved_job_duplicate_cannot_overwrite_attempt_or_cross_session(
    bound, tool, argument
):
    harness, started = bound
    sid = await session(harness)
    other_sid = await session(harness)
    jid = new_reserved_job_id()
    payload = {"job_id": jid, "tool": tool, "async_": True, **argument}
    first = await call(harness, sid, payload)
    assert first.ok and first.result["mode"] == "job"
    assert first.result["result"]["job_id"] == jid
    assert first.result["result"]["session_id"] == sid
    with use_state_store(harness.executor.services.state_store):
        paths = attempt_paths(jid, 1)
        original = paths["command"].read_bytes()
        paths["log"].write_text("first log", encoding="utf-8")
        before_scripts = set(paths["command"].parent.glob("python-script-*.py"))
    duplicate = await call(harness, sid, payload)
    foreign = await call(harness, other_sid, payload)
    assert not duplicate.ok and not foreign.ok
    assert "already in use" in duplicate.error.message
    assert paths["command"].read_bytes() == original
    assert (
        set(paths["command"].parent.glob("python-script-*.py"))
        == before_scripts
    )
    assert paths["log"].read_text(encoding="utf-8") == "first log"
    assert len(started) == 1
    poll = await harness.call(
        harness.executor_id, "job", {"poll": [jid], "lines": 1}, session_id=sid
    )
    assert poll.ok
    assert poll.result["outputs"][0]["job"]["job_id"] == jid
    assert poll.result["outputs"][0]["output"].strip() == "first log"
    poll_other = await harness.call(
        harness.executor_id,
        "job",
        {"poll": [jid], "lines": 1},
        session_id=other_sid,
    )
    assert not poll_other.ok


@pytest.mark.asyncio
async def test_concurrent_reserved_id_only_one_native_start(bound):
    harness, started = bound
    sid, other_sid = await session(harness), await session(harness)
    payload = {
        "job_id": new_reserved_job_id(),
        "tool": "bash",
        "command": "echo one",
        "async_": True,
    }
    results = await asyncio.gather(
        call(harness, sid, payload), call(harness, other_sid, payload)
    )
    assert sum(result.ok for result in results) == 1
    assert len(started) == 1


@pytest.mark.asyncio
async def test_reserved_job_requires_valid_private_tracked_request(bound):
    harness, started = bound
    sid = await session(harness)
    valid = {
        "job_id": new_reserved_job_id(),
        "tool": "bash",
        "command": "echo one",
        "async_": True,
    }
    invalid = [
        {**valid, "job_id": "../../escape"},
        {**valid, "pty": True},
        {**valid, "async_": False},
        {**valid, "async_": "true"},
        {**valid, "code": "also Python"},
        {**valid, "session_id": sid},
        {**valid, "tool": "write_file"},
    ]
    for payload in invalid:
        assert not (await call(harness, sid, payload)).ok
    assert not (
        await harness.call(harness.executor_id, JOB_START_RESERVED_OP, valid)
    ).ok
    assert not started


@pytest.mark.asyncio
async def test_reserved_persisted_job_uses_original_id_and_waiter(
    bound, monkeypatch
):
    from workgate.schemas.result_models.jobs import JobTailOutput

    harness, started = bound
    sid = await session(harness)
    jid = new_reserved_job_id()

    async def wait(session_id, job_id, timeout_s):
        assert session_id == sid and job_id == jid
        assert 0 <= timeout_s <= 1
        with job_shell._store_transaction() as store:
            row = job_shell._find_session_job(store, sid, jid)
            return JobTailOutput(
                job=job_shell._public_job(row), output="working"
            )

    monkeypatch.setattr(harness.executor.shell.jobs, "wait", wait)
    result = await call(
        harness,
        sid,
        {
            "job_id": jid,
            "tool": "bash",
            "command": "echo work",
            "persist_on_timeout": True,
            "timeout_s": 1,
        },
    )
    assert result.ok
    assert result.result["mode"] == "job"
    assert result.result["result"]["job_id"] == jid
    assert result.result["result"]["persisted"] is True
    assert result.result["result"]["stdout"] == "working"
    assert len(started) == 1
