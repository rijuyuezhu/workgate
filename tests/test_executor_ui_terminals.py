from __future__ import annotations

from typing import Any, cast

import pytest

import workgate.executor.ui_terminals as ui_terminals
from workgate.executor.shell_service import ShellService
from workgate.executor.ui_terminals import UiTerminalsService


class _FakeShell:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def list_all(self) -> str:
        self.calls.append(("list_all", None))
        return "listed"

    async def start_unowned(self, args: dict[str, Any]) -> str:
        self.calls.append(("start_unowned", args))
        return "started"

    async def send_unowned(self, args: dict[str, Any]) -> str:
        self.calls.append(("send_unowned", args))
        return "sent"

    async def resize_unowned(self, args: dict[str, Any]) -> str:
        self.calls.append(("resize_unowned", args))
        return "resized"

    async def read_unowned(self, args: dict[str, Any]) -> str:
        self.calls.append(("read_unowned", args))
        return "read"

    async def kill_unowned(self, args: dict[str, Any]) -> str:
        self.calls.append(("kill_unowned", args))
        return "killed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op", "method", "expected"),
    [
        ("ui.terminals.list", "list_all", "listed"),
        ("ui.terminals.start", "start_unowned", "started"),
        ("ui.terminals.send", "send_unowned", "sent"),
        ("ui.terminals.resize", "resize_unowned", "resized"),
        ("ui.terminals.read", "read_unowned", "read"),
        ("ui.terminals.kill", "kill_unowned", "killed"),
    ],
)
async def test_ui_terminal_service_routes_shell_operations(
    op: str, method: str, expected: str
) -> None:
    shell = _FakeShell()
    service = UiTerminalsService(cast(ShellService, shell))
    args = {"shell_id": "shell-1"}

    result = await service.execute(op, args)

    assert result == expected
    assert shell.calls == [(method, None if method == "list_all" else args)]


@pytest.mark.asyncio
async def test_ui_terminal_service_routes_bridge_operations(
    monkeypatch,
) -> None:
    shell = _FakeShell()
    service = UiTerminalsService(cast(ShellService, shell))
    calls: list[tuple[Any, ...]] = []

    async def opened(shell_id: str, cols: int, rows: int) -> str:
        calls.append(("open", shell_id, cols, rows))
        return "opened"

    async def read(bridge_id: str, max_bytes: int, wait_ms: int) -> str:
        calls.append(("read", bridge_id, max_bytes, wait_ms))
        return "read"

    async def written(bridge_id: str, data_b64: str) -> str:
        calls.append(("write", bridge_id, data_b64))
        return "written"

    async def resized(bridge_id: str, cols: int, rows: int) -> str:
        calls.append(("resize", bridge_id, cols, rows))
        return "resized"

    async def closed(bridge_id: str) -> str:
        calls.append(("close", bridge_id))
        return "closed"

    monkeypatch.setattr(ui_terminals, "open_terminal_bridge_execute", opened)
    monkeypatch.setattr(ui_terminals, "read_terminal_bridge_execute", read)
    monkeypatch.setattr(ui_terminals, "write_terminal_bridge_execute", written)
    monkeypatch.setattr(ui_terminals, "resize_terminal_bridge_execute", resized)
    monkeypatch.setattr(ui_terminals, "close_terminal_bridge_execute", closed)

    assert (
        await service.execute(
            "ui.terminals.bridge.open",
            {"shell_id": "shell-1", "cols": "101", "rows": "42"},
        )
        == "opened"
    )
    assert (
        await service.execute(
            "ui.terminals.bridge.read",
            {"bridge_id": "bridge-1", "max_bytes": "99", "wait_ms": "7"},
        )
        == "read"
    )
    assert (
        await service.execute(
            "ui.terminals.bridge.write",
            {"bridge_id": "bridge-1", "data_b64": "eA=="},
        )
        == "written"
    )
    assert (
        await service.execute(
            "ui.terminals.bridge.resize",
            {"bridge_id": "bridge-1", "cols": "80", "rows": "24"},
        )
        == "resized"
    )
    assert (
        await service.execute(
            "ui.terminals.bridge.close", {"bridge_id": "bridge-1"}
        )
        == "closed"
    )

    assert calls == [
        ("open", "shell-1", 101, 42),
        ("read", "bridge-1", 99, 7),
        ("write", "bridge-1", "eA=="),
        ("resize", "bridge-1", 80, 24),
        ("close", "bridge-1"),
    ]


@pytest.mark.asyncio
async def test_ui_terminal_service_rejects_unknown_operation() -> None:
    service = UiTerminalsService(cast(ShellService, _FakeShell()))

    with pytest.raises(
        NotImplementedError, match="unsupported executor UI terminal"
    ):
        await service.execute("ui.terminals.unknown", {})
