import logging

import workgate.control.server as control_server
from workgate.config.control import resolve_control_config
from workgate.config.settings import Settings


def test_run_uvicorn_uses_explicit_shared_server_contract(monkeypatch):
    config = resolve_control_config(
        Settings(
            host="127.0.0.2",
            port=9123,
            log_level="info",
            forwarded_allow_ips="127.0.0.1,10.0.0.0/8",
        )
    )
    app = object()
    calls = []
    monkeypatch.setattr(
        control_server.uvicorn,
        "run",
        lambda built_app, **kwargs: calls.append((built_app, kwargs)),
    )

    control_server.run_uvicorn(app, config=config)

    assert calls == [
        (
            app,
            {
                "host": "127.0.0.2",
                "port": 9123,
                "forwarded_allow_ips": "127.0.0.1,10.0.0.0/8",
                "timeout_graceful_shutdown": 10,
                "log_level": "info",
            },
        )
    ]


def test_configure_runtime_logging_applies_resolved_level(monkeypatch):
    basic_levels = []
    root = logging.getLogger()
    previous_level = root.level
    monkeypatch.setattr(
        control_server.logging,
        "basicConfig",
        lambda *, level: basic_levels.append(level),
    )

    try:
        control_server.configure_runtime_logging("debug")
        assert basic_levels == [logging.DEBUG]
        assert root.level == logging.DEBUG
    finally:
        root.setLevel(previous_level)
