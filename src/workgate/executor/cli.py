"""Public CLI for the final Workgate executor process."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from ..agent_bridge.redaction import _redact_text
from ..config.cli import register_config_and_setting_args, settings_from_args
from ..persistence import get_state_store
from ..protocol.errors import ProtocolErrorCode
from .config import resolve_executor_config
from .control_client import ExecutorControlClient, ExecutorControlError
from .pairing import (
    ExecutorPairingClient,
    build_pair_start_request,
    persist_profile_and_validate,
    wait_for_pairing,
)
from .profile import ExecutorProfileStore, normalize_control_url
from .runtime import build_executor_runtime
from .service import ExecutorServiceManager, ExecutorServiceStatus


def _run_async(coro: Any) -> Any:
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        print(
            f"Status: executor command failed: {_redact_text(str(exc))}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1) from None


async def _connect(args: argparse.Namespace) -> None:
    settings_from_args(args, configure=True)
    profile_store = ExecutorProfileStore(get_state_store())
    control_url = normalize_control_url(str(args.control_url))
    existing = profile_store.load()
    existing_executor_id: str | None = None

    if existing is not None:
        if existing.control_url != control_url:
            raise RuntimeError(
                "executor profile already belongs to a different control URL"
            )
        client = ExecutorControlClient(existing)
        try:
            await client.validate()
        except ExecutorControlError as exc:
            if exc.code not in {
                ProtocolErrorCode.UNAUTHORIZED_EXECUTOR,
                ProtocolErrorCode.EXECUTOR_REVOKED,
            }:
                raise
            existing_executor_id = existing.executor_id
        else:
            print(
                f"Already paired as {existing.executor_id}; profile remains valid.",
                flush=True,
            )
            return
        finally:
            await client.aclose()

    pairing_client = ExecutorPairingClient(control_url)
    try:
        started = await pairing_client.start(
            build_pair_start_request(
                requested_name=args.name,
                existing_executor_id=existing_executor_id,
            )
        )
        print(f"Open: {started.verification_uri}", flush=True)
        print(f"Code: {started.user_code}", flush=True)
        if existing_executor_id is not None:
            print(
                f"Existing executor ID: {existing_executor_id}",
                flush=True,
            )
        result = await wait_for_pairing(pairing_client, started)
    finally:
        await pairing_client.aclose()

    profile = await persist_profile_and_validate(
        control_url=control_url,
        pairing_result=result,
        profile_store=profile_store,
    )
    print(f"Paired executor: {profile.executor_id}", flush=True)


async def _run(args: argparse.Namespace) -> None:
    if bool(getattr(args, "managed_service", False)):
        for name in tuple(os.environ):
            if name.startswith("WORKGATE_"):
                os.environ.pop(name, None)
    settings = settings_from_args(args, configure=True)
    runtime = build_executor_runtime(resolve_executor_config(settings))
    async with runtime.lifespan():
        connection = runtime.connection
        if connection is None:
            raise RuntimeError(
                "executor is not paired; run `workgate executor connect <control-url>` first"
            )
        owner_action = await connection.wait_owner_action()
        raise owner_action


def _connect_from_args(args: argparse.Namespace) -> None:
    _run_async(_connect(args))


def _run_from_args(args: argparse.Namespace) -> None:
    _run_async(_run(args))


def _service_manager(args: argparse.Namespace) -> ExecutorServiceManager:
    settings = settings_from_args(args, configure=True)
    return ExecutorServiceManager(settings)


def _redact_service_text(value: str) -> str:
    return _redact_text(value)


def _print_service_status(
    manager: ExecutorServiceManager, status: ExecutorServiceStatus
) -> None:
    print(f"State: {status.state.value}")
    print(f"Backend: {status.backend}")
    if status.service_file:
        print(f"Service: {status.service_file}")
    if status.log_path:
        print(f"Log: {status.log_path}")
    print(
        "Runtime: current"
        if status.runtime_current
        else "Runtime: stale; run `workgate executor install-service` to refresh"
    )
    if status.detail:
        print(f"Detail: {_redact_service_text(status.detail)}")


def _run_service_action(args: argparse.Namespace, action: str) -> None:
    try:
        manager = _service_manager(args)
        if action == "install":
            result = manager.install()
            _print_service_status(manager, result.status)
            return
        if action == "uninstall":
            status = manager.uninstall()
            _print_service_status(manager, status)
            return
        if action == "status":
            status = manager.status()
            _print_service_status(manager, status)
            return
        if action == "start":
            status = manager.start()
            _print_service_status(manager, status)
            return
        if action == "stop":
            status = manager.stop()
            _print_service_status(manager, status)
            return
        if action == "restart":
            status = manager.restart()
            _print_service_status(manager, status)
            return
        if action == "logs":
            output = manager.logs(lines=int(args.lines))
            if output:
                print(_redact_service_text(output))
            return
        raise AssertionError(action)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        print(
            f"Status: executor command failed: {_redact_text(str(exc))}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1) from None


def register_executor_cli(subparsers: Any) -> argparse.ArgumentParser:
    """Register final executor provisioning and runtime commands."""
    executor = subparsers.add_parser(
        "executor",
        help="Pair or run this machine as a Workgate executor",
        description="Pair or run the final Workgate executor process.",
    )
    actions = executor.add_subparsers(dest="executor_command", required=True)

    connect = actions.add_parser(
        "connect",
        help="Pair this machine with a control service",
    )
    connect.add_argument("control_url")
    connect.add_argument("--name", default=None)
    register_config_and_setting_args(connect)
    connect.set_defaults(handler=_connect_from_args)

    run = actions.add_parser(
        "run", help="Run using the stored executor profile"
    )
    run.add_argument(
        "--managed-service",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    register_config_and_setting_args(run)
    run.set_defaults(handler=_run_from_args)

    lifecycle: tuple[tuple[str, str, str], ...] = (
        (
            "install-service",
            "Install and start the per-user executor service",
            "install",
        ),
        ("status", "Show the managed executor service status", "status"),
        ("start", "Start the installed executor service", "start"),
        ("stop", "Stop the installed executor service", "stop"),
        ("restart", "Restart the installed executor service", "restart"),
        (
            "uninstall-service",
            "Remove the per-user executor service",
            "uninstall",
        ),
    )
    for name, help_text, action in lifecycle:
        parser = actions.add_parser(name, help=help_text)
        register_config_and_setting_args(parser)
        parser.set_defaults(
            handler=lambda args, action=action: _run_service_action(
                args, action
            )
        )

    logs = actions.add_parser(
        "logs", help="Show bounded recent executor service logs"
    )
    logs.add_argument(
        "--lines",
        type=int,
        default=100,
        help="Number of recent lines to show (1-1000)",
    )
    register_config_and_setting_args(logs)
    logs.set_defaults(handler=lambda args: _run_service_action(args, "logs"))
    return executor
