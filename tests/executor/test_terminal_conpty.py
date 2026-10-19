"""Executor ConPTY startup failure and shutdown paths, portable unit tests."""

import asyncio
import threading
from dataclasses import replace
from typing import Any, cast

import pytest

from workgate.config.executor import resolve_executor_config
from workgate.config.settings import get_settings
from workgate.executor.terminal import conpty as shell_conpty
from workgate.executor.terminal.runtime import (
    build_terminal_runtime,
    use_terminal_runtime,
)
from workgate.platform import windows_conpty
from workgate.platform.windows_conpty import ConPtyRegistry, ConPtySession


class FakePty:
    def __init__(self) -> None:
        self.alive = True
        self.read_event = threading.Event()
        self.closed = False

    def isalive(self) -> bool:
        return self.alive

    def read(self) -> str:
        self.read_event.wait(timeout=2)
        return ""

    def close(self, *, force: bool) -> None:
        assert force
        self.closed = True
        self.alive = False
        self.read_event.set()


@pytest.mark.asyncio
async def test_cancel_during_conpty_creation_reaps_new_shell(
    monkeypatch, tmp_path
):
    started = threading.Event()
    release = threading.Event()
    created: list[FakePty] = []

    def delayed_spawn(_argv, **_kwargs) -> ConPtySession:
        started.set()
        assert release.wait(timeout=5)
        process = FakePty()
        created.append(process)
        return ConPtySession(process)

    monkeypatch.setattr(windows_conpty, "spawn_conpty", delayed_spawn)
    monkeypatch.setattr(
        shell_conpty.shutil, "which", lambda *_args, **_kw: "powershell.exe"
    )
    registry = ConPtyRegistry()
    config = replace(
        resolve_executor_config(get_settings()),
        shell_executable="powershell.exe",
    )
    with windows_conpty.use_conpty_registry(registry):
        task = asyncio.create_task(
            shell_conpty.start(
                config,
                cast(Any, None),
                "cancelled",
                str(tmp_path),
                None,
                None,
                {},
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert registry.active() == {}
    assert len(created) == 1 and created[0].closed
    registry.close()


@pytest.mark.asyncio
async def test_executor_runtime_close_reaps_conpty_shell(monkeypatch):
    created: list[FakePty] = []

    def fake_spawn(_argv, **_kw):
        process = FakePty()
        created.append(process)
        return ConPtySession(process)

    monkeypatch.setattr(windows_conpty, "spawn_conpty", fake_spawn)
    runtime = build_terminal_runtime()
    await runtime.start()
    with use_terminal_runtime(runtime):
        runtime.conpty.start("shell", ["powershell.exe"], cwd=".", limit=2)
        assert runtime.conpty.get("shell")
    await runtime.aclose()
    assert created[0].closed
    assert runtime.conpty.active() == {}
    with pytest.raises(RuntimeError, match="closed"):
        runtime.conpty.start("new", ["powershell.exe"], cwd=".", limit=2)


@pytest.mark.asyncio
async def test_conpty_refuses_missing_interactive_shell(monkeypatch, tmp_path):
    monkeypatch.setattr(
        shell_conpty.shutil, "which", lambda *_args, **_kw: None
    )
    config = replace(
        resolve_executor_config(get_settings()),
        shell_executable="not-installed.exe",
    )
    with (
        windows_conpty.use_conpty_registry(ConPtyRegistry()),
        pytest.raises(FileNotFoundError, match="not found"),
    ):
        await shell_conpty.start(
            config,
            cast(Any, None),
            "missing",
            str(tmp_path),
            None,
            None,
            {},
        )
