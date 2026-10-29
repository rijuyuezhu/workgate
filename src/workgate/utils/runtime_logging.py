"""Shared standard-library runtime logging for role entrypoints."""

import logging

_LOG_LEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}


def configure_runtime_logging(log_level: str) -> None:
    """Apply the configured level without adding duplicate root handlers."""
    level = _LOG_LEVELS[log_level]
    logging.basicConfig(level=level)
    logging.getLogger().setLevel(level)
