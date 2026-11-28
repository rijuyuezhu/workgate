"""Control-owned generation driver lifecycle and request coalescing."""

import asyncio
from contextvars import ContextVar

import pytest

from workgate.hosted_chat.generation_supervisor import (
    GenerationCapacityExceeded,
    GenerationSupervisor,
)


@pytest.mark.asyncio
async def test_supervisor_coalesces_and_drains_authenticated_drivers():
    supervisor = GenerationSupervisor(global_limit=2, owner_limit=1)
    principal = ContextVar("principal", default="none")
    started = asyncio.Event()
    finish = asyncio.Event()
    stopped = []

    async def execute():
        owner = principal.get()
        started.set()
        try:
            await finish.wait()
            return True
        finally:
            stopped.append(owner)

    principal.set("alice")
    assert supervisor.start("alice", "run", execute)
    assert not supervisor.start("alice", "run", execute)
    await started.wait()
    assert supervisor.active("alice", "run")
    with pytest.raises(GenerationCapacityExceeded):
        supervisor.start("alice", "other", execute)
    principal.set("bob")
    assert supervisor.start("bob", "run", execute)
    with pytest.raises(GenerationCapacityExceeded):
        supervisor.start("charlie", "run", execute)
    await asyncio.sleep(0)
    await supervisor.aclose()
    assert sorted(stopped) == ["alice", "bob"]
    assert not supervisor.active("alice", "run")
    await supervisor.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        supervisor.start("alice", "run", execute)


@pytest.mark.asyncio
async def test_supervisor_releases_completed_and_failed_slots(caplog):
    supervisor = GenerationSupervisor(global_limit=1, owner_limit=1)

    async def fail():
        raise ValueError("secret-provider-token")

    async def succeed():
        return True

    with pytest.raises(ValueError, match="identity"):
        supervisor.start("", "run", succeed)
    assert supervisor.start("alice", "run", fail)
    await asyncio.sleep(0)
    assert not supervisor.active("alice", "run")
    # Replace a completed task before its done callback runs. The old callback
    # must never remove the replacement driver.
    assert supervisor.start("alice", "run", succeed)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert "secret-provider-token" not in caplog.text
    assert "ValueError" in caplog.text
    assert not supervisor.active("alice", "run")
    await supervisor.aclose()


@pytest.mark.parametrize("global_limit,owner_limit", [(0, 1), (2, 0), (1, 2)])
def test_invalid_supervisor_capacity(global_limit, owner_limit):
    with pytest.raises(ValueError, match="capacity"):
        GenerationSupervisor(global_limit=global_limit, owner_limit=owner_limit)


@pytest.mark.asyncio
async def test_wait_idle_does_not_cancel_suspending_driver():
    supervisor = GenerationSupervisor()
    release = asyncio.Event()

    async def execute():
        await release.wait()
        return True

    assert supervisor.start("alice", "run", execute)
    assert await supervisor.wait_idle("bob", "run", timeout=0)
    assert not await supervisor.wait_idle("alice", "run", timeout=0)
    assert supervisor.active("alice", "run")
    release.set()
    assert await supervisor.wait_idle("alice", "run")
    assert not supervisor.active("alice", "run")
    await supervisor.aclose()
