"""Shared standard-library runtime logging for role entrypoints."""

import logging

_LOG_LEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}


class _WorkgateInfoFilter(logging.Filter):
    """Keep third-party INFO/DEBUG records out of shared root handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        return (
            record.levelno >= logging.WARNING
            or record.name == "workgate"
            or record.name.startswith("workgate.")
        )


_INFO_FILTER = _WorkgateInfoFilter()


def configure_runtime_logging(log_level: str) -> None:
    """Apply the configured level without adding duplicate root handlers."""
    level = _LOG_LEVELS[log_level]
    # Third-party INFO/DEBUG logs may contain request URLs or other payloads.
    root_level = max(level, logging.WARNING)
    logging.basicConfig(
        level=root_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    root = logging.getLogger()
    root.setLevel(root_level)
    logging.getLogger("workgate").setLevel(level)
    for handler in root.handlers:
        if _INFO_FILTER not in handler.filters:
            handler.addFilter(_INFO_FILTER)
