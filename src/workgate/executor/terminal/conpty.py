"""Small persistent-shell operations over the Executor-owned ConPTY runtime."""

import asyncio
import re
import shutil

from ...audit import audit
from ...config.executor import ExecutorConfig
from ...platform.shell import interactive_shell_argv, shell_argv
from ...platform.windows_conpty import conpty_registry
from ...schemas.result_models.shell import (
    KillPersistentShellOutput,
    ListPersistentShellsOutput,
    PersistentShellInfo,
    ReadPersistentShellOutput,
    ResizePersistentShellOutput,
    SendPersistentShellInputOutput,
    StartPersistentShellOutput,
)
from ..tool_session.store import ToolSessionStore


async def start(
    config: ExecutorConfig,
    store: ToolSessionStore,
    shell_id: str,
    cwd: str,
    command: str | None,
    owner: str | None,
    env: dict[str, str],
) -> StartPersistentShellOutput:
    registry = conpty_registry()
    shell = str(config.shell_executable).strip() or "powershell.exe"
    if shutil.which(shell, path=env.get("PATH")) is None:
        raise FileNotFoundError(f"Configured Windows shell not found: {shell}")
    argv = (
        shell_argv(shell, command, system="Windows")
        if command is not None
        else interactive_shell_argv(shell)
    )
    spawn = asyncio.create_task(
        asyncio.to_thread(
            registry.start,
            shell_id,
            argv,
            cwd=cwd,
            limit=max(1, config.max_tmux_sessions),
            owner=owner,
            env=env,
        )
    )
    try:
        await asyncio.shield(spawn)
    except BaseException:
        # Startup may finish after the caller is cancelled. Do not leak PTYs.
        while not spawn.done():
            try:
                await asyncio.shield(spawn)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not spawn.cancelled() and spawn.exception() is None:
            await asyncio.to_thread(registry.kill, shell_id)
        raise
    audit("start_persistent_shell", shell_id=shell_id, backend="conpty")
    return StartPersistentShellOutput(
        shell_id=shell_id, cwd=cwd, command=command or shell, backend="conpty"
    )


async def send(
    shell_id: str, text: str, enter: bool
) -> SendPersistentShellInputOutput:
    session = conpty_registry().get(shell_id)
    await asyncio.to_thread(session.write, text + ("\r" if enter else ""))
    audit(
        "send_persistent_shell_input",
        shell_id=shell_id,
        bytes=len(text.encode("utf-8")),
        enter=enter,
        backend="conpty",
    )
    return SendPersistentShellInputOutput(
        shell_id=shell_id, sent_bytes=len(text.encode("utf-8")), enter=enter
    )


async def resize(
    shell_id: str, cols: int, rows: int
) -> ResizePersistentShellOutput:
    await asyncio.to_thread(conpty_registry().get(shell_id).resize, cols, rows)
    audit(
        "resize_persistent_shell",
        shell_id=shell_id,
        cols=cols,
        rows=rows,
        backend="conpty",
    )
    return ResizePersistentShellOutput(
        shell_id=shell_id, cols=cols, rows=rows, resized=True, backend="conpty"
    )


async def read(
    shell_id: str, lines: int, preserve_ansi: bool
) -> ReadPersistentShellOutput:
    session = conpty_registry().get(shell_id)
    with session.lock:
        output = bytes(session.tail).decode("utf-8", errors="replace")
    if not preserve_ansi:
        output = re.sub(
            r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", output
        )
    return ReadPersistentShellOutput(
        shell_id=shell_id,
        output="\n".join(output.splitlines()[-max(1, lines) :]),
        lines=lines,
        backend="conpty",
    )


async def kill(
    store: ToolSessionStore, shell_id: str
) -> KillPersistentShellOutput:
    success = await asyncio.to_thread(conpty_registry().kill, shell_id)
    if success:
        store.release_persistent_shell(shell_id)
    audit(
        "kill_persistent_shell", shell_id=shell_id, ok=success, backend="conpty"
    )
    return KillPersistentShellOutput(
        shell_id=shell_id,
        killed=success,
        backend="conpty",
    )


async def inventory(
    store: ToolSessionStore, preserve: set[str] | None = None
) -> tuple[ListPersistentShellsOutput, bool]:
    registry = conpty_registry()
    active = await asyncio.to_thread(registry.active)
    store.reconcile_persistent_shells(set(active) | set(preserve or ()))
    return (
        ListPersistentShellsOutput(
            shells=[
                PersistentShellInfo.model_validate(
                    {
                        "shell_id": key,
                        "cwd": entry.cwd,
                        "command": entry.command,
                        "attached": bool(entry.subscribers),
                        "backend": "conpty",
                    }
                )
                for key, entry in active.items()
            ]
        ),
        True,
    )


async def owned(owner: str) -> list[str]:
    active = await asyncio.to_thread(conpty_registry().active)
    return [key for key, entry in active.items() if entry.owner == owner]
