"""Native WorkGate persistent ConPTY ShellService + existing WebUI bridge."""

import asyncio
import base64
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("Native Windows Python is required")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workgate.config.executor import resolve_executor_config  # noqa: E402
from workgate.config.settings import clear_settings_cache, get_settings  # noqa: E402
from workgate.executor.services import build_runtime_services  # noqa: E402
from workgate.executor.shell import (  # noqa: E402
    kill_persistent_shell_execute,
    list_persistent_shells_execute,
    read_persistent_shell_output_execute,
    resize_persistent_shell_execute,
    send_persistent_shell_input_execute,
    start_persistent_shell_execute,
)
from workgate.executor.terminal.bridge import (  # noqa: E402
    close_terminal_bridge_execute,
    open_terminal_bridge_execute,
    read_terminal_bridge_execute,
    write_terminal_bridge_execute,
)
from workgate.executor.terminal.runtime import (  # noqa: E402
    build_terminal_runtime,
    use_terminal_runtime,
)
from workgate.persistence import use_state_store  # noqa: E402


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="workgate-conpty-shell-") as temp:
        root = Path(temp)
        os.environ["WORKGATE_DEFAULT_WORKDIR"] = str(root)
        os.environ["WORKGATE_STATE_DIR"] = str(root / "state")
        clear_settings_cache()
        config = resolve_executor_config(get_settings())
        services = build_runtime_services(config)
        store = services.tool_session_store
        runtime = build_terminal_runtime()
        await runtime.start()
        with (
            use_terminal_runtime(runtime),
            use_state_store(services.state_store),
        ):
            try:
                started = await start_persistent_shell_execute(
                    config, store, cwd=str(root), name="win-smoke"
                )
                assert started.backend == "conpty", started
                listed = await list_persistent_shells_execute(config, store)
                assert [item.shell_id for item in listed.shells] == [
                    "win-smoke"
                ], listed

                # The configured shell limit is exact: no second live PTY.
                limited = replace(config, max_tmux_sessions=1)
                try:
                    await start_persistent_shell_execute(
                        limited, store, cwd=str(root), name="must-not-start"
                    )
                except RuntimeError as exc:
                    assert "session limit" in str(exc), str(exc)
                else:
                    raise AssertionError(
                        "Windows ConPTY session limit bypassed"
                    )

                first = await open_terminal_bridge_execute("win-smoke")
                second = await open_terminal_bridge_execute("win-smoke")
                assert first["backend"] == second["backend"] == "conpty"
                await send_persistent_shell_input_execute(
                    config, "win-smoke", "Write-Output WIN_BRIDGE_386"
                )
                for bridge in (first, second):
                    buffer = b""
                    for _ in range(110):
                        item = await read_terminal_bridge_execute(
                            bridge["bridge_id"]
                        )
                        buffer += base64.b64decode(item["data_b64"])
                        if b"WIN_BRIDGE_386" in buffer:
                            break
                    assert b"WIN_BRIDGE_386" in buffer, repr(buffer[-2000:])

                captured = await read_persistent_shell_output_execute(
                    config, "win-smoke"
                )
                assert "WIN_BRIDGE_386" in captured.output, captured

                # WebUI may split one UTF-8 code point across frames. The
                # attachment must decode incrementally without corruption.
                chinese = "Write-Output '你好-CONPTY_386'\r".encode()
                split = chinese.index("你".encode()) + 1
                for chunk in (chinese[:split], chinese[split:]):
                    result = await write_terminal_bridge_execute(
                        first["bridge_id"],
                        base64.b64encode(chunk).decode("ascii"),
                    )
                    assert result["written_bytes"] == len(chunk)
                buffer = b""
                for _ in range(110):
                    item = await read_terminal_bridge_execute(
                        second["bridge_id"]
                    )
                    buffer += base64.b64decode(item["data_b64"])
                    if "你好-CONPTY_386".encode() in buffer:
                        break
                assert "你好-CONPTY_386".encode() in buffer, repr(
                    buffer[-2000:]
                )

                await close_terminal_bridge_execute(first["bridge_id"])
                resized = await resize_persistent_shell_execute(
                    config, "win-smoke", 100, 32
                )
                assert resized.resized and resized.backend == "conpty"
                await send_persistent_shell_input_execute(
                    config, "win-smoke", "Write-Output WIN_STILL_LIVE"
                )
                captured = await read_persistent_shell_output_execute(
                    config, "win-smoke"
                )
                # Read is a snapshot; raw subscribers are waited for separately.
                buffer = b""
                for _ in range(110):
                    item = await read_terminal_bridge_execute(
                        second["bridge_id"]
                    )
                    buffer += base64.b64decode(item["data_b64"])
                    if b"WIN_STILL_LIVE" in buffer:
                        break
                assert b"WIN_STILL_LIVE" in buffer, repr(buffer[-2000:])
                await close_terminal_bridge_execute(second["bridge_id"])
                killed = await kill_persistent_shell_execute(
                    config, store, "win-smoke"
                )
                assert killed.killed and killed.backend == "conpty"
                assert not (
                    await list_persistent_shells_execute(config, store)
                ).shells
            finally:
                await runtime.aclose()
        # The interactive backend is PowerShell-based, not a cmd.exe
        # compatibility shim. Fail before creating a PTY for cmd.
        cmd_runtime = build_terminal_runtime()
        await cmd_runtime.start()
        try:
            with (
                use_terminal_runtime(cmd_runtime),
                use_state_store(services.state_store),
            ):
                cmd_config = replace(config, shell_executable="cmd.exe")
                try:
                    await start_persistent_shell_execute(
                        cmd_config, store, cwd=str(root), name="unsupported-cmd"
                    )
                except ValueError as exc:
                    assert "requires PowerShell" in str(exc)
                else:
                    raise AssertionError("cmd.exe interactive PTY was accepted")
                assert not cmd_runtime.conpty.active()
        finally:
            await cmd_runtime.aclose()

        restarted = build_terminal_runtime()
        await restarted.start()
        try:
            with (
                use_terminal_runtime(restarted),
                use_state_store(services.state_store),
            ):
                # ConPTY does not survive Executor restart like detached tmux.
                assert not (
                    await list_persistent_shells_execute(config, store)
                ).shells
                assert not restarted.conpty.active()
        finally:
            await restarted.aclose()
    print("Native WorkGate ConPTY Shell, 2 bridges, limit and restart: PASS")


if __name__ == "__main__":
    asyncio.run(main())
