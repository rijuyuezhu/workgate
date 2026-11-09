"""Executor protocol owns the authorized native Desktop stream lifecycle."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.executor.test_gui_service import FakeGuiBackend
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import Settings
from workgate.executor.gui import stream as stream_module
from workgate.executor.profile import ExecutorProfile
from workgate.executor.runtime import build_executor_runtime
from workgate.protocol.credentials import new_executor_credential
from workgate.protocol.executor import ExecutorCommand
from workgate.protocol.ids import new_command_id, new_executor_id

_STREAM_ID = "stream_abcdefghijklmnopqrstuvwxyz"
_SESSION_ID = "sess_0000000000000000000001"


def _runtime(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(default_workdir=workspace, state_dir=tmp_path / "state")
        )
    )
    runtime.services.tool_session_store.create_session(
        session_id=_SESSION_ID, workdir=workspace
    )
    runtime.gui._backend = FakeGuiBackend()
    return runtime


def _command(
    *, session_id: str | None = None, args: dict[str, Any] | None = None
):
    return ExecutorCommand(
        id=new_command_id(),
        op="gui.stream.attach",
        session_id=session_id,
        args={
            "stream_id": _STREAM_ID,
            "session_id": _SESSION_ID,
            "window_id": "window-1",
        }
        if args is None
        else args,
    )


@pytest.mark.asyncio
async def test_desktop_attach_owns_task_and_cleans_up_on_executor_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    profile = ExecutorProfile(
        control_url="https://control.example",
        executor_id=new_executor_id(),
        credential=new_executor_credential(),
    )
    assert runtime.profile_store is not None
    runtime.profile_store.save(profile)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class FakeStream:
        stream_id = _STREAM_ID

        async def run(self) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    async def connect(
        p: ExecutorProfile, gui, *, stream_id, session_id, window_id
    ):
        assert p == profile and gui is runtime.gui
        assert (stream_id, session_id, window_id) == (
            _STREAM_ID,
            _SESSION_ID,
            "window-1",
        )
        return FakeStream()

    monkeypatch.setattr(
        stream_module, "connect_executor_desktop_stream", connect
    )
    result = await runtime._execute_protocol_command(_command())
    assert result == {"stream_id": _STREAM_ID, "connected": True}
    await asyncio.wait_for(started.wait(), timeout=1)
    assert len(runtime._desktop_stream_tasks) == 1
    await runtime.aclose()
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert not runtime._desktop_stream_tasks


@pytest.mark.asyncio
async def test_desktop_attach_rejects_unpaired_or_foreign_session(
    tmp_path: Path,
):
    runtime = _runtime(tmp_path)
    try:
        with pytest.raises(ValueError, match="sessionless"):
            await runtime._execute_protocol_command(
                _command(session_id=_SESSION_ID)
            )
        with pytest.raises(RuntimeError, match="paired"):
            await runtime._execute_protocol_command(_command())
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ({"session_id": ""}, "invalid"),
        ({"window_id": ""}, "invalid"),
        ({"window_id": "\u7a97" * 600}, "invalid"),
        ({"window_id": "nonexistent-window"}, "unavailable"),
        ({"stream_id": None}, "must be str"),
    ],
)
async def test_desktop_attach_rejects_invalid_payloads(
    tmp_path: Path, replacement: dict, message: str
) -> None:
    runtime = _runtime(tmp_path)
    assert runtime.profile_store is not None
    runtime.profile_store.save(
        ExecutorProfile(
            control_url="https://control.example",
            executor_id=new_executor_id(),
            credential=new_executor_credential(),
        )
    )
    args = {
        "stream_id": _STREAM_ID,
        "session_id": _SESSION_ID,
        "window_id": "window-1",
        **replacement,
    }
    try:
        with pytest.raises(ValueError, match=message):
            await runtime._execute_protocol_command(_command(args=args))
        assert not runtime._desktop_stream_tasks
    finally:
        await runtime.aclose()
