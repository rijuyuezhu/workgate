"""Native process group lifecycle must be bounded and fail closed."""

import signal
from typing import Any, cast

import pytest

from workgate.platform import process as platform_process
from workgate.platform.posix import process as posix_process


class FakeProcess:
    pid = 812

    def __init__(self, *, timeout_count=0):
        self.timeout_count = timeout_count
        self.waits = 0
        self.actions = []

    async def wait(self):
        self.waits += 1
        if self.waits <= self.timeout_count:
            raise TimeoutError
        return 0

    def terminate(self):
        self.actions.append("terminate")

    def kill(self):
        self.actions.append("kill")


@pytest.mark.parametrize(
    ("timeout_count", "expected_signals", "expected_result"),
    [
        (0, [signal.SIGTERM], ""),
        (1, [signal.SIGTERM, signal.SIGKILL], ""),
        (
            2,
            [signal.SIGTERM, signal.SIGKILL],
            "Process did not exit after SIGKILL",
        ),
    ],
)
@pytest.mark.asyncio
async def test_posix_process_group_termination_escalates_on_timeout(
    monkeypatch, timeout_count, expected_signals, expected_result
):
    proc = FakeProcess(timeout_count=timeout_count)
    signals = []
    monkeypatch.setattr(posix_process.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        posix_process.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )
    assert (
        await platform_process.terminate_process_tree(
            cast(Any, proc),
            system="Linux",
            graceful_timeout_s=1,
            kill_timeout_s=1,
        )
        == expected_result
    )
    assert signals == [(812, sig) for sig in expected_signals]
    assert proc.waits == (2 if timeout_count > 0 else 1)
    assert proc.actions == []


@pytest.mark.asyncio
async def test_process_group_termination_falls_back_to_direct_child(
    monkeypatch,
):
    proc = FakeProcess(timeout_count=1)
    monkeypatch.setattr(
        posix_process.os,
        "getpgid",
        lambda _pid: (_ for _ in ()).throw(ProcessLookupError()),
    )
    assert (
        await platform_process.terminate_process_tree(
            cast(Any, proc), system="Darwin"
        )
        == ""
    )
    assert proc.actions == ["terminate", "kill"]
    assert proc.waits == 2
