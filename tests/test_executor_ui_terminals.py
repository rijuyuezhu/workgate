from typing import Any, cast

import pytest

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
@pytest.mark.parametrize(
    "op",
    [
        "ui.terminals.bridge.open",
        "ui.terminals.bridge.read",
        "ui.terminals.bridge.write",
        "ui.terminals.bridge.resize",
        "ui.terminals.bridge.close",
    ],
)
async def test_ui_terminal_service_rejects_legacy_bridge_operations(
    op: str,
) -> None:
    service = UiTerminalsService(cast(ShellService, _FakeShell()))

    with pytest.raises(
        NotImplementedError, match="unsupported executor UI terminal"
    ):
        await service.execute(op, {})


@pytest.mark.asyncio
async def test_ui_terminal_service_rejects_unknown_operation() -> None:
    service = UiTerminalsService(cast(ShellService, _FakeShell()))

    with pytest.raises(
        NotImplementedError, match="unsupported executor UI terminal"
    ):
        await service.execute("ui.terminals.unknown", {})
