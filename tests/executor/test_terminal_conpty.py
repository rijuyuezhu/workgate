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


@pytest.mark.asyncio
async def test_windows_persistent_shell_operations_reuse_executor_registry(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    import workgate.executor.shell as shell_ops
    from workgate.platform.host import HostOS

    class InteractivePty(FakePty):
        def __init__(self):
            super().__init__()
            self.writes = []
            self.sizes = []

        def write(self, text):
            self.writes.append(text)

        def setwinsize(self, rows, cols):
            self.sizes.append((rows, cols))

    processes = []

    def fake_spawn(_argv, **_kwargs):
        process = InteractivePty()
        processes.append(process)
        return ConPtySession(process)

    monkeypatch.setattr(windows_conpty, "spawn_conpty", fake_spawn)
    monkeypatch.setattr(
        shell_conpty.shutil, "which", lambda *_a, **_k: "powershell.exe"
    )
    monkeypatch.setattr(shell_ops, "host_os", lambda: HostOS.WINDOWS)
    config = replace(
        resolve_executor_config(get_settings()),
        shell_executable="powershell.exe",
    )
    reconciled = []
    released = []
    store = SimpleNamespace(
        reconcile_persistent_shells=lambda ids: reconciled.append(ids),
        release_persistent_shell=lambda sid: released.append(sid),
    )
    runtime = build_terminal_runtime()
    await runtime.start()
    try:
        with use_terminal_runtime(runtime):
            started = await shell_ops._start_persistent_shell_locked(
                config,
                cast(Any, store),
                cwd=str(tmp_path),
                name="hello",
                owner_session_id="owner1",
            )
            assert started.backend == "conpty"
            assert await shell_ops.list_owned_persistent_shell_ids_execute(
                config, cast(Any, store), "owner1"
            ) == ["hello"]
            sent = await shell_ops.send_persistent_shell_input_execute(
                config, "hello", "echo hi", enter=True
            )
            assert sent.sent_bytes == 7
            await shell_ops.send_persistent_shell_input_execute(
                config, "hello", "more", enter=False
            )
            assert processes[0].writes == ["echo hi\r", "more"]
            resized = await shell_ops.resize_persistent_shell_execute(
                config, "hello", 100, 30
            )
            assert resized.backend == "conpty"
            assert processes[0].sizes == [(30, 100)]
            with runtime.conpty.get("hello").lock:
                runtime.conpty.get("hello").tail.extend(
                    b"\x1b[31mred\x1b[0m\nsecond"
                )
            plain = await shell_ops.read_persistent_shell_output_execute(
                config, "hello", 1
            )
            color = await shell_ops.read_persistent_shell_output_execute(
                config, "hello", 2, preserve_ansi=True
            )
            assert plain.output == "second"
            assert "\x1b[31m" in color.output
            listed = await shell_ops.list_persistent_shells_execute(
                config, cast(Any, store)
            )
            assert [item.shell_id for item in listed.shells] == ["hello"]
            assert reconciled[-1] == {"hello"}
            killed = await shell_ops.kill_persistent_shell_execute(
                config, cast(Any, store), "hello"
            )
            assert killed.killed and released == ["hello"]
            missing = await shell_ops.kill_persistent_shell_execute(
                config, cast(Any, store), "hello"
            )
            assert not missing.killed
            assert not (
                await shell_ops.list_persistent_shells_execute(
                    config, cast(Any, store)
                )
            ).shells
            with pytest.raises(ValueError, match="reserved"):
                await shell_ops._start_persistent_shell_locked(
                    config,
                    cast(Any, store),
                    cwd=str(tmp_path),
                    name="hello",
                    forbidden_shell_ids=frozenset({"hello"}),
                )
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_conpty_explicit_command_start_and_missing_shell_rejection(
    monkeypatch, tmp_path
):
    created = []
    monkeypatch.setattr(
        windows_conpty,
        "spawn_conpty",
        lambda argv, **_kwargs: (
            created.append(argv) or ConPtySession(FakePty())
        ),
    )
    monkeypatch.setattr(
        shell_conpty.shutil, "which", lambda *_a, **_k: "powershell.exe"
    )
    config = replace(
        resolve_executor_config(get_settings()),
        shell_executable="powershell.exe",
    )
    registry = ConPtyRegistry()
    with windows_conpty.use_conpty_registry(registry):
        started = await shell_conpty.start(
            config,
            cast(Any, None),
            "custom",
            str(tmp_path),
            "Write-Output 'ok'",
            None,
            {},
        )
        assert started.backend == "conpty"
        assert "-Command" in created[0]
    registry.close()
