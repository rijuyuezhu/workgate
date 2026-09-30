"""Shared HTTP server runtime behavior for control transports."""

import logging
from typing import Any

import uvicorn

from ..config.control import ControlConfig

_GRACEFUL_SHUTDOWN_TIMEOUT_S = 10
_LOG_LEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}


def configure_runtime_logging(log_level: str) -> None:
    """Apply one resolved control log level to Workgate runtime logging."""
    level = _LOG_LEVELS[log_level]
    logging.basicConfig(level=level)
    logging.getLogger().setLevel(level)


def run_uvicorn(app: Any, *, config: ControlConfig) -> None:
    """Run one control-owned ASGI app with the shared server contract."""
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        forwarded_allow_ips=config.forwarded_allow_ips,
        timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_TIMEOUT_S,
        log_level=config.log_level,
    )
