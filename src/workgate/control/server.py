"""Shared HTTP server runtime behavior for control transports."""

from typing import Any

import uvicorn

from ..config.control import ControlConfig

_GRACEFUL_SHUTDOWN_TIMEOUT_S = 10


def run_uvicorn(app: Any, *, config: ControlConfig) -> None:
    """Run the ordinary HTTP host; supervised roles report ready after bind."""
    from ..supervisor import is_child, notify_ready

    options: dict[str, Any] = dict(
        host=config.host,
        port=config.port,
        forwarded_allow_ips=config.forwarded_allow_ips,
        timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_TIMEOUT_S,
        log_level=config.log_level,
        access_log=False,  # Request paths may contain sensitive query strings.
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
