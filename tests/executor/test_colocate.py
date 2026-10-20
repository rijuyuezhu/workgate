"""Colocate uses the same one-Parent supervisor as individual CLI roles."""

import argparse
import sys

import pytest

from workgate import supervisor
from workgate.colocate import cli as colocate


def test_colocate_uses_one_parent_with_two_normal_role_children(monkeypatch):
    calls = []
    monkeypatch.setattr(
        colocate, "run_parent", lambda commands: calls.append(commands)
    )
    colocate.run_colocate(
        argparse.Namespace(
            control_config="control.yaml", executor_config="executor.yaml"
        )
    )
    assert calls == [
        [
            supervisor.ChildCommand(
                "control",
                (
                    sys.executable,
                    "-m",
                    "workgate.main",
                    "control",
                    "--config",
                    "control.yaml",
                ),
            ),
            supervisor.ChildCommand(
                "executor",
                (
                    sys.executable,
                    "-m",
                    "workgate.main",
                    "executor",
                    "run",
                    "--config",
                    "executor.yaml",
                ),
            ),
        ]
    ]


def test_colocate_defaults_to_existing_role_commands(monkeypatch):
    calls = []
    monkeypatch.setattr(
        colocate, "run_parent", lambda commands: calls.extend(commands)
    )
    colocate.run_colocate(
        argparse.Namespace(control_config=None, executor_config=None)
    )
    assert calls == [
        supervisor.ChildCommand(
            "control", (sys.executable, "-m", "workgate.main", "control")
        ),
        supervisor.ChildCommand(
            "executor",
            (sys.executable, "-m", "workgate.main", "executor", "run"),
        ),
    ]


def test_colocate_frozen_executable_launches_same_binary(monkeypatch):
    monkeypatch.setattr(supervisor.sys, "frozen", True, raising=False)
    assert colocate._child_command("control") == [sys.executable, "control"]


def test_colocate_does_not_accept_duplicate_role_commands():
    with pytest.raises(ValueError, match="unique"):
        supervisor.supervise(
            [
                supervisor.ChildCommand("control", (sys.executable,)),
                supervisor.ChildCommand("control", (sys.executable,)),
            ]
        )
