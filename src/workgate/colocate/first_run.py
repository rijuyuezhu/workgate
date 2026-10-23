"""Pair the local Executor against temporary Control before installing Colocate."""

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from ..app_paths import app_paths
from ..config.control import CONTROL_SETTING_NAMES
from ..config.settings import load_settings
from ..control.first_run import initialize_control_config
from ..platform.process import noninteractive_creationflags

_READY_TIMEOUT_S = 45


def _local_host(host: str) -> str:
    if host in {"127.0.0.1", "localhost"}:
        return "127.0.0.1"
    if host == "::1":
        return "[::1]"
    raise RuntimeError(
        "Colocate setup requires a loopback-only Control bind; "
        "configure remote access after pairing"
    )


def _wait_ready(url: str, process: subprocess.Popen[bytes]) -> None:
    # Never proxy a health check for a local service.
    opener = build_opener(ProxyHandler({}))
    until = time.monotonic() + _READY_TIMEOUT_S
    while time.monotonic() < until:
        if process.poll() is not None:
            raise RuntimeError("temporary Control exited before readiness")
        try:
            with opener.open(f"{url}/readyz", timeout=0.5) as response:
                if (
                    response.status == 200
                    and response.read(1024) == b'{"ok":true}'
                ):
                    return
        except OSError:
            pass
        time.sleep(0.2)
    raise TimeoutError("temporary Control did not become ready in 45 seconds")


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        process.wait(timeout=2)
        return
    try:
        process.wait(timeout=12)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _connect(args: argparse.Namespace, url: str) -> None:
    from ..main import _build_parser

    arguments = ["executor", "connect", url]
    if args.executor_config:
        arguments.extend(["--config", args.executor_config])
    if args.name:
        arguments.extend(["--name", args.name])
    parsed = _build_parser().parse_args(arguments)
    parsed.handler(parsed)


def _reject_existing_services(args: argparse.Namespace) -> None:
    """Do not install another role alongside existing native services."""
    from ..config.executor import (
        EXECUTOR_SETTING_NAMES,
        resolve_executor_config,
    )
    from ..control import service as control_service
    from ..executor.service import ExecutorServiceManager
    from . import service as colocate_service

    if sys.platform == "win32":
        installed = (
            control_service._task().status() is not None
            or colocate_service._windows_task().status() is not None
        )
    elif sys.platform == "linux":
        control = control_service._unit()
        colocate = colocate_service.systemd_user_unit_path(
            colocate_service._systemctl(
                "show", "--property=UnitPath", "--value"
            ).stdout,
            colocate_service._SERVICE_NAME,
        )
        installed = any(
            path.exists() or path.is_symlink() for path in (control, colocate)
        )
    else:
        raise RuntimeError("Colocate setup requires Linux or Windows services")
    if installed:
        raise RuntimeError(
            "Control or Colocate native service already installed; "
            "uninstall it explicitly before setup"
        )
    paths = app_paths()
    settings = load_settings(
        args.executor_config,
        setting_names=EXECUTOR_SETTING_NAMES,
        default_config_path=paths.executor_config_file,
        default_overrides={"state_dir": paths.executor_state_dir},
    )
    if (
        ExecutorServiceManager(resolve_executor_config(settings))
        .status()
        .installed
    ):
        raise RuntimeError(
            "separate Executor service already installed; "
            "uninstall it explicitly before setup"
        )


def setup_colocate(args: argparse.Namespace) -> None:
    """Owner approval happens before any persistent native service installation."""
    if getattr(sys, "frozen", False):
        raise RuntimeError(
            "Colocate first-run setup requires a Python source installation"
        )
    if any(key.startswith("WORKGATE_") for key in os.environ):
        raise RuntimeError(
            "Colocate setup needs durable YAML, not WORKGATE_* overrides"
        )
    _reject_existing_services(args)
    control_file = (
        Path(args.control_config or app_paths().config_file)
        .expanduser()
        .absolute()
    )
    initialize_control_config(path=control_file)
    settings = load_settings(control_file, setting_names=CONTROL_SETTING_NAMES)
    if not settings.ui_enabled:
        raise RuntimeError(
            "Colocate setup requires Control WebUI for owner approval"
        )
    host = _local_host(settings.host)
    url = f"http://{host}:{settings.port}"

    # Refuse to attach to or interrupt any pre-existing listener.
    try:
        with socket.create_connection(
            (host.strip("[]"), settings.port), timeout=0.5
        ):
            raise RuntimeError(
                "Control port is occupied; stop the existing service first"
            )
    except ConnectionRefusedError:
        pass

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "workgate.colocate.temporary_control",
            str(control_file),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        creationflags=noninteractive_creationflags(),
    )
    try:
        _wait_ready(url, process)
        print(
            f"Temporary Control: {settings.resolved_base_url.rstrip('/')}{settings.ui_path}",
            flush=True,
        )
        print(
            "Approve the Executor pairing in the WebUI with your private admin PIN.",
            flush=True,
        )
        _connect(args, url)
    finally:
        _stop(process)

    from .service import install_colocate_service

    install_colocate_service(args)
