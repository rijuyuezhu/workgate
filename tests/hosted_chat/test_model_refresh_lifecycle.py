"""Control-owned model refresh lifecycle, with no actual provider traffic."""

import asyncio

import pytest

from tests.support.hosted_chat_http import provider, runtime_for
from workgate.control import runtime as control_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,ui_enabled", [(False, True), (True, False)])
async def test_disabled_host_does_not_start_refresh(
    tmp_path, enabled, ui_enabled, monkeypatch
):
    runtime = runtime_for(tmp_path, enabled=enabled, ui_enabled=ui_enabled)
    provider(runtime)
    calls = []

    async def refresh(repo):
        calls.append(repo)

    monkeypatch.setattr(control_runtime, "refresh_model_channels", refresh)
    async with runtime.lifespan():
        await asyncio.sleep(0)
        assert runtime._model_refresh_task is None
    assert not calls


@pytest.mark.asyncio
async def test_unused_host_does_not_create_database(tmp_path):
    runtime = runtime_for(tmp_path)
    async with runtime.lifespan():
        await asyncio.sleep(0)
        assert runtime._model_refresh_task is not None
        assert not (runtime.config.state_dir / "hosted-chat").exists()
    assert runtime._model_refresh_task is None


@pytest.mark.asyncio
async def test_refresh_owned_and_cancelled_before_control_shutdown(
    tmp_path, monkeypatch
):
    runtime = runtime_for(tmp_path)
    provider(runtime)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def refresh(repo):
        assert repo.profile("test-provider")["models"] == ["test-model"]
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(control_runtime, "refresh_model_channels", refresh)
    async with runtime.lifespan():
        await asyncio.wait_for(entered.wait(), timeout=5)
        task = runtime._model_refresh_task
        assert task is not None
        await runtime.start()
        assert runtime._model_refresh_task is task
    assert cancelled.is_set()
    assert task.done() and task.cancelled()
    assert runtime._model_refresh_task is None
