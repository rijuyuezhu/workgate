"""Public CLI for the final Workgate executor process."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from ..agent_bridge.redaction import _redact_text
from ..config.cli import register_config_and_setting_args, settings_from_args
from ..persistence import get_state_store
from ..protocol.errors import ProtocolErrorCode
from .config import resolve_executor_config
from .control_client import ExecutorControlClient, ExecutorControlError
from .hello import build_executor_hello
from .pairing import (
    ExecutorPairingClient,
    build_pair_start_request,
    persist_profile_before_first_hello,
    wait_for_pairing,
)
from .profile import ExecutorProfileStore, normalize_control_url
from .runtime import build_executor_runtime


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
    settings = settings_from_args(args, configure=True)
    config = resolve_executor_config(settings)
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
            await client.hello(build_executor_hello(config))
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

    profile = await persist_profile_before_first_hello(
        control_url=control_url,
        pairing_result=result,
        profile_store=profile_store,
        config=config,
    )
    print(f"Paired executor: {profile.executor_id}", flush=True)


async def _run(args: argparse.Namespace) -> None:
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
    register_config_and_setting_args(run)
    run.set_defaults(handler=_run_from_args)
    return executor
