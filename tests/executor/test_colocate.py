"""The colocate command only manages ordinary role child processes."""

import argparse
import signal
import subprocess
import sys

import pytest

from workgate.colocate import cli as colocate


class FakeChild:
    def __init__(self, status: int | None) -> None:
        self.status = status
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.status

    def terminate(self) -> None:
        self.terminated = True
        self.status = 0

    def kill(self) -> None:
        self.killed = True
        self.status = -9

    def wait(self, timeout: float | None = None) -> int:
        _ = timeout
        assert self.status is not None
        return self.status


def test_colocate_runs_normal_role_commands_and_forwards_configs(monkeypatch):
    control, executor = FakeChild(9), FakeChild(None)
    calls: list[list[str]] = []

    def spawn(argv):
        calls.append(argv)
        return (control, executor)[len(calls) - 1]

    monkeypatch.setattr(colocate.subprocess, "Popen", spawn)
    with pytest.raises(SystemExit) as result:
        colocate.run_colocate(
            argparse.Namespace(
                control_config="control.yaml",
                executor_config="executor.yaml",
            )
        )

    assert result.value.code == 9
    assert calls == [
        [
            sys.executable,
            "-m",
            "workgate.main",
            "control",
            "--config",
            "control.yaml",
        ],
        [
            sys.executable,
            "-m",
            "workgate.main",
            "executor",
            "run",
            "--config",
            "executor.yaml",
        ],
    ]
    assert executor.terminated


def test_colocate_uses_role_defaults_and_stops_on_interrupt(monkeypatch):
    children = [FakeChild(None), FakeChild(None)]
    calls: list[list[str]] = []

    def spawn(argv):
        calls.append(argv)
        return children[len(calls) - 1]

    monkeypatch.setattr(colocate.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        colocate.time,
        "sleep",
        lambda _delay: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    with pytest.raises(SystemExit) as result:
        colocate.run_colocate(
            argparse.Namespace(control_config=None, executor_config=None)
        )

    assert result.value.code == 130
    assert calls == [
        [sys.executable, "-m", "workgate.main", "control"],
        [sys.executable, "-m", "workgate.main", "executor", "run"],
    ]
    assert all(child.terminated for child in children)


def test_colocate_frozen_executable_launches_same_binary(monkeypatch):
    monkeypatch.setattr(colocate.sys, "frozen", True, raising=False)
    assert colocate._child_command("control") == [sys.executable, "control"]


def test_colocate_kills_unresponsive_child(monkeypatch):
    class HungChild(FakeChild):
        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout=None) -> int:
            if not self.killed:
                raise subprocess.TimeoutExpired("executor", timeout or 5)
            return super().wait(timeout)

    children = [FakeChild(1), HungChild(None)]
    spawned = []

    def spawn(_argv):
        child = children[len(spawned)]
        spawned.append(child)
        return child

    monkeypatch.setattr(colocate.subprocess, "Popen", spawn)
    with pytest.raises(SystemExit, match="1"):
        colocate.run_colocate(
            argparse.Namespace(control_config=None, executor_config=None)
        )
    assert children[1].killed


def test_colocate_forwards_sigterm_cleanup(monkeypatch):
    children = [FakeChild(None), FakeChild(None)]
    created = []

    def spawn(_argv):
        child = children[len(created)]
        created.append(child)
        return child

    def trigger_sigterm(_delay):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(colocate.subprocess, "Popen", spawn)
    monkeypatch.setattr(colocate.time, "sleep", trigger_sigterm)
    with pytest.raises(SystemExit) as result:
        colocate.run_colocate(
            argparse.Namespace(control_config=None, executor_config=None)
        )

    assert result.value.code == 143
    assert all(child.terminated for child in children)


def test_colocate_reaps_both_when_child_exits_during_termination(monkeypatch):
    class ExitedChild(FakeChild):
        def terminate(self) -> None:
            self.status = 0
            raise ProcessLookupError

    children = [ExitedChild(None), FakeChild(None)]
    spawned = []

    def spawn(_argv):
        child = children[len(spawned)]
        spawned.append(child)
        return child

    monkeypatch.setattr(colocate.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        colocate.time,
        "sleep",
        lambda _delay: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    with pytest.raises(SystemExit) as result:
        colocate.run_colocate(
            argparse.Namespace(control_config=None, executor_config=None)
        )

    assert result.value.code == 130
    assert children[1].terminated


def test_colocate_cleans_up_control_if_executor_cannot_start(monkeypatch):
    control = FakeChild(None)
    calls = 0

    def spawn(_argv):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("failed to spawn executor")
        return control

    monkeypatch.setattr(colocate.subprocess, "Popen", spawn)
    with pytest.raises(OSError, match="failed to spawn executor"):
        colocate.run_colocate(
            argparse.Namespace(control_config=None, executor_config=None)
        )

    assert control.terminated
