"""Browser Human UI API namespace helpers."""

UI_API_PREFIX = "/api/ui"


def is_ui_api_path(path: str) -> bool:
    """Return whether a path belongs to the Human UI API namespace."""
    return path == UI_API_PREFIX or path.startswith(UI_API_PREFIX + "/")
