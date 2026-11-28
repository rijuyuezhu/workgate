"""Shared bounded regex engine from pinned v6.0.3 memory_protection.py.

Source blob d9ca3d3a86f765d2d06d77bf40209bc359f1d94d; see LICENSE.upstream.
"""

import re
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=256)
def _compile(expression: str, flags: int = 0) -> Any:
    import regex  # WebUI optional extra; never block a plain Controller wheel at import time.

    if not isinstance(expression, str) or not 0 < len(expression) <= 512:
        raise ValueError("Invalid memory content pattern")
    # RE2JS does not accept lookarounds, recursive groups, or backreferences.
    if re.search(r"\\[1-9]|\(\?[=!]|\(\?<[=!]|\(\?P[=<]", expression):
        raise ValueError("Unsupported regular expression syntax")
    return regex.compile(expression, flags | regex.VERSION1)


def _rules(policy: dict) -> list[tuple[Any, str]]:
    catalog = {
        "sk_prefix": (r"\b(sk-)[a-zA-Z0-9_-]+", 0, "sk- prefix token"),
        "bearer_header": (r'\b(Bearer )[^\s"\']+', re.I, "Bearer token"),
        "api_key_header": (
            r'\b(api-key:?\s+)[^\s"\']+',
            re.I,
            "api-key header",
        ),
    }
    starters = policy.get("starterPatterns")
    if starters is None:
        starters = list(catalog)
    customs = policy.get("customPatterns") or []
    if not isinstance(starters, list) or len(starters) > 256:
        raise ValueError("Invalid starter patterns")
    if not isinstance(customs, list) or len(customs) > 256:
        raise ValueError("Invalid custom patterns")
    compiled = []
    for item in starters:
        if not isinstance(item, str) or item not in catalog:
            raise ValueError("Invalid starter pattern id")
        pattern, flags, label = catalog[item]
        compiled.append((_compile(pattern, flags), label))
    total_length = 0
    for item in customs:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not 0 < len(item["id"]) <= 128
        ):
            raise ValueError("Invalid custom pattern")
        if (
            not isinstance(item.get("label"), str)
            or not 0 < len(item["label"]) <= 256
        ):
            raise ValueError("Invalid custom pattern label")
        expression = item.get("regex")
        if not isinstance(expression, str):
            raise ValueError("Invalid custom pattern expression")
        total_length += len(expression)
        if total_length > 8192:
            raise ValueError("Custom pattern budget exceeded")
        compiled.append((_compile(expression), item["label"]))
    return compiled
