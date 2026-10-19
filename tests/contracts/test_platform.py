"""Native backend selection must fail closed for unimplemented hosts."""

from typing import Any, cast

import pytest

from workgate.platform.host import HostOS, host_os
from workgate.platform.posix.terminal import UnixTmuxAttach
from workgate.platform.process import (
    new_process_group_kwargs,
    terminate_process_tree,
)
from workgate.platform.terminal import (
    supports_raw_terminal_host,
    terminal_attachment_factory,
)
from workgate.platform.windows_conpty import WindowsConPtyAttach
from workgate.protocol.terminal import TerminalBridgeUnsupportedError


@pytest.mark.parametrize(
    ("system", "expected"),
    [
        ("Linux", HostOS.LINUX),
        ("Darwin", HostOS.MACOS),
        ("Windows", HostOS.WINDOWS),
        ("Haiku", None),
        ("", None),
    ],
)
def test_host_os_does_not_guess(system, expected):
    assert host_os(system) == expected


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_posix_capability_contracts(system):
    assert new_process_group_kwargs(system=system) == {
        "start_new_session": True
    }
    assert supports_raw_terminal_host(system=system)
    assert terminal_attachment_factory(system=system) is UnixTmuxAttach


def test_windows_terminal_factory_reuses_native_attachment(monkeypatch):
    from workgate.platform import windows_conpty

    monkeypatch.setattr(windows_conpty, "is_available", lambda: True)
    assert supports_raw_terminal_host(system="Windows")
    assert terminal_attachment_factory(system="Windows") is WindowsConPtyAttach


def test_windows_terminal_capability_missing_dependency_fails_closed(
    monkeypatch,
):
    from workgate.platform import windows_conpty

    monkeypatch.setattr(windows_conpty, "is_available", lambda: False)
    assert not supports_raw_terminal_host(system="Windows")
    with pytest.raises(TerminalBridgeUnsupportedError, match="unsupported"):
        terminal_attachment_factory(system="Windows")


def test_unsupported_host_fails_closed():
    assert not supports_raw_terminal_host(system="Haiku")
    with pytest.raises(RuntimeError, match="containment is unsupported"):
        new_process_group_kwargs(system="Haiku")
    with pytest.raises(TerminalBridgeUnsupportedError, match="unsupported"):
        terminal_attachment_factory(system="Haiku")


@pytest.mark.asyncio
async def test_windows_termination_refuses_uncontained_process():
    with pytest.raises(RuntimeError, match="not assigned to a Job Object"):
        await terminate_process_tree(cast(Any, None), system="Windows")
