import logging
import re
import subprocess
import sys

import workgate.control.server as control_server
import workgate.utils.runtime_logging as runtime_logging
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
                "access_log": False,
            },
        )
    ]


def test_configure_runtime_logging_applies_resolved_level(monkeypatch):
    basic_levels = []
    root = logging.getLogger()
    previous_level = root.level
    workgate = logging.getLogger("workgate")
    previous_workgate_level = workgate.level
    existing_filters = {
        handler: tuple(handler.filters) for handler in root.handlers
    }
    monkeypatch.setattr(
        runtime_logging.logging,
        "basicConfig",
        lambda **kwargs: basic_levels.append(kwargs),
    )

    try:
        runtime_logging.configure_runtime_logging("debug")
        assert basic_levels == [
            {
                "level": logging.WARNING,
                "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
                "datefmt": "%Y-%m-%dT%H:%M:%S%z",
            }
        ]
        assert root.level == logging.WARNING
        assert workgate.level == logging.DEBUG
    finally:
        root.setLevel(previous_level)
        workgate.setLevel(previous_workgate_level)
        for handler, filters in existing_filters.items():
            handler.filters[:] = filters


def test_runtime_logging_reuses_existing_root_handlers(caplog):
    root = logging.getLogger()
    existing = tuple(root.handlers)
    previous_level = root.level
    workgate = logging.getLogger("workgate")
    previous_workgate_level = workgate.level
    existing_filters = {
        handler: tuple(handler.filters) for handler in root.handlers
    }
    try:
        runtime_logging.configure_runtime_logging("info")
        runtime_logging.configure_runtime_logging("warning")
        assert tuple(root.handlers) == existing
        assert all(
            handler.filters.count(runtime_logging._INFO_FILTER) == 1
            for handler in existing
        )
        assert root.level == logging.WARNING
        assert workgate.level == logging.WARNING
        assert (
            logging.getLogger("workgate.executor.connection").isEnabledFor(
                logging.INFO
            )
            is False
        )
        runtime_logging.configure_runtime_logging("info")
        assert workgate.level == logging.INFO
        assert logging.getLogger("workgate.executor.connection").isEnabledFor(
            logging.INFO
        )
        runtime_logging.configure_runtime_logging("debug")
        assert workgate.level == logging.DEBUG
        assert root.level == logging.WARNING
        assert all(
            handler.filters.count(runtime_logging._INFO_FILTER) == 1
            for handler in existing
        )
        logging.getLogger("workgate.executor.runtime").info("startup probe")
        assert "startup probe" in caplog.text
    finally:
        root.setLevel(previous_level)
        workgate.setLevel(previous_workgate_level)
        for handler, filters in existing_filters.items():
            handler.filters[:] = filters


def test_runtime_logging_emits_timestamp_level_and_module_from_fresh_process():
    # A new role process has no pytest/uvicorn handlers overriding basicConfig.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import logging; "
            "from workgate.utils.runtime_logging import configure_runtime_logging; "
            "configure_runtime_logging('info'); "
            "logging.getLogger('workgate.executor.connection').info('connected')",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not result.stdout
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} "
        r"INFO workgate\.executor\.connection: connected\n",
        result.stderr,
    )


def test_default_info_reports_workgate_events_but_not_third_party_requests():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import logging; "
            "from workgate.config.settings import Settings; "
            "from workgate.utils.runtime_logging import configure_runtime_logging; "
            "configure_runtime_logging(Settings().log_level); "
            "logging.getLogger('workgate.executor.connection').info('connected'); "
            "logging.getLogger('httpx').setLevel(logging.INFO); "
            "logging.getLogger('httpx').info('HTTP Request: https://example.test/?token=raw-secret'); "
            "logging.getLogger('httpx').warning('retryable problem')",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "INFO workgate.executor.connection: connected" in result.stderr
    assert "raw-secret" not in result.stderr
    assert "INFO httpx" not in result.stderr
    assert "WARNING httpx: retryable problem" in result.stderr
