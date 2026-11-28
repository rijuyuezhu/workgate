"""Shared validated source envelope fields."""

import re
from typing import Any

_TOKEN = re.compile(r"^[A-Za-z0-9._~:/+=-]{1,256}$")


class EventConflict(ValueError):
    pass


def _string(value: Any, label: str, limit: int = 131_072) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode()) > limit
    ):
        raise ValueError(
            f"{label} must be a nonempty string of at most {limit} bytes"
        )
    return value
