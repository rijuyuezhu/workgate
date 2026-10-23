"""Executor-only first-run composes existing pairing and user-service commands."""

import argparse

import pytest

from workgate.executor import cli
from workgate.main import _build_parser


def test_setup_cli_contract_has_explicit_control_url_and_durable_paths():
    args = _build_parser().parse_args(
        [
            "executor",
            "setup",
            "https://control.test",
            "--name",
            "Laptop",
            "--state-dir",
            "/tmp/paired-executor",
        ]
    )
    assert args.handler is cli._setup_from_args
    assert args.control_url == "https://control.test"
    assert args.name == "Laptop"
    assert str(args.state_dir) == "/tmp/paired-executor"


def test_setup_waits_for_approval_before_install(monkeypatch):
    calls = []

    def connect(args):
        calls.append(("connect", args.control_url))

    def service(args, action):
        calls.append(("service", action))

    monkeypatch.setattr(cli, "_connect_from_args", connect)
    monkeypatch.setattr(cli, "_run_service_action", service)
    args = argparse.Namespace(control_url="https://control.test")
    cli._setup_from_args(args)
    assert calls == [
        ("connect", "https://control.test"),
        ("service", "install"),
    ]


def test_setup_pairing_failure_never_installs_service(monkeypatch):
    def reject(_args):
        raise SystemExit(1)

    def forbidden(_args, _action):
        raise AssertionError("must not install before pairing approval")

    monkeypatch.setattr(cli, "_connect_from_args", reject)
    monkeypatch.setattr(cli, "_run_service_action", forbidden)
    with pytest.raises(SystemExit) as caught:
        cli._setup_from_args(
            argparse.Namespace(control_url="https://control.test")
        )
    assert caught.value.code == 1


def test_setup_pairing_and_service_reuse_exact_same_selected_config(
    monkeypatch,
):
    args = _build_parser().parse_args(
        [
            "executor",
            "setup",
            "https://control.test",
            "--config",
            "/tmp/executor-config.yaml",
            "--state-dir",
            "/tmp/private-executor",
        ]
    )
    captures = []

    def connect(value):
        captures.append(("connect", value.config, str(value.state_dir)))

    def install(value, action):
        captures.append((action, value.config, str(value.state_dir)))

    monkeypatch.setattr(cli, "_connect_from_args", connect)
    monkeypatch.setattr(cli, "_run_service_action", install)
    args.handler(args)
    assert captures == [
        ("connect", "/tmp/executor-config.yaml", "/tmp/private-executor"),
        ("install", "/tmp/executor-config.yaml", "/tmp/private-executor"),
    ]
