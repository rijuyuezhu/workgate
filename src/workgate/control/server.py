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
    """Run the ordinary HTTP host; supervised roles report ready after bind."""
    from ..supervisor import is_child, notify_ready

    options: dict[str, Any] = dict(
        host=config.host,
        port=config.port,
        forwarded_allow_ips=config.forwarded_allow_ips,
        timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_TIMEOUT_S,
        log_level=config.log_level,
    )
    if not is_child():
        uvicorn.run(app, **options)
        return

    class _ReadyServer(uvicorn.Server):
        async def startup(self, sockets: list[Any] | None = None) -> None:
            await super().startup(sockets=sockets)
            if self.started:
                from ..config.inspection import current_startup_config

                snapshot = current_startup_config("control")
                notify_ready(
                    None if snapshot is None else snapshot.config_path,
                    None if snapshot is None else snapshot.startup_file_digest,
                )

    _ReadyServer(uvicorn.Config(app, **options)).run()
