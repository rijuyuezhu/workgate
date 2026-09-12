"""Redaction helpers for agent bridge configuration, errors, and payloads."""

import json
import re
from typing import Any

from .models import SENSITIVE_KEY_PATTERN, SENSITIVE_KEY_RE

SENSITIVE_ARG_RE = re.compile(
    rf"(?P<prefix>--?[A-Za-z0-9_.-]*{SENSITIVE_KEY_PATTERN}[A-Za-z0-9_.-]*=)\S+",
    re.I,
)
SENSITIVE_SPACED_ARG_RE = re.compile(
    rf"(?P<prefix>(?:^|\s)--?[A-Za-z0-9_.-]*{SENSITIVE_KEY_PATTERN}"
    rf"[A-Za-z0-9_.-]*\s+)\S+",
    re.I,
)
SENSITIVE_FLAG_RE = re.compile(
    rf"^--?[A-Za-z0-9_.-]*{SENSITIVE_KEY_PATTERN}[A-Za-z0-9_.-]*$",
    re.I,
)
TEXT_SENSITIVE_KEY_PATTERN = (
    r"(?:[A-Za-z0-9_.-]*(?:authorization|cookie|credentials?|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|token|secret|password|passwd)[A-Za-z0-9_.-]*|key)"
)
SENSITIVE_TEXT_QUOTED_VALUE_RE = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_.-])(?P<key_quote>['\"]?)"
    rf"{TEXT_SENSITIVE_KEY_PATTERN}(?P=key_quote)\s*[:=]\s*)"
    r"(?P<value_quote>['\"])(?P<value>[^'\"]*)(?P=value_quote)",
    re.I,
)
SENSITIVE_TEXT_UNQUOTED_VALUE_RE = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_.-])(?P<key_quote>['\"]?)"
    rf"{TEXT_SENSITIVE_KEY_PATTERN}(?P=key_quote)\s*[:=]\s*)"
    r"(?P<value>[^\s,;'\"\)\}\]\n][^,;'\"\)\}\]\n]*)",
    re.I,
)
SENSITIVE_HEADER_VALUE_RE = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_.-])(?P<key_quote>['\"]?)"
    r"(?:authorization|proxy-authorization|cookie|set-cookie)(?P=key_quote)"
    r"[^\S\r\n]*:[^\S\r\n]*)"
    r"(?P<value>[^'\"\r\n][^\r\n]*)",
    re.I,
)
SENSITIVE_QUOTED_ARG_LIST_RE = re.compile(
    rf"(?P<prefix>(?P<flag_quote>['\"])--?[A-Za-z0-9_.-]*"
    rf"{SENSITIVE_KEY_PATTERN}[A-Za-z0-9_.-]*(?P=flag_quote)\s*,\s*"
    r"(?P<value_quote>['\"]))(?P<value>[^'\"]*)(?P=value_quote)",
    re.I,
)
BEARER_TOKEN_RE = re.compile(r"\bBearer\s+[^\s,;'\"\)\}\]]+", re.I)
HIGH_CONFIDENCE_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16})\b"
)
URL_USERINFO_PASSWORD_RE = re.compile(
    r"(?P<prefix>https?://[^/\s:@?#]+:)[^@/\s?#]+(?=@)", re.I
)
URL_QUERY_RE = re.compile(r"(?P<prefix>https?://[^\s?]+)\?[^\s\"')]+", re.I)


def redact_mapping(value: Any) -> Any:
    """Recursively redact mapping values whose keys are likely to contain credentials."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if SENSITIVE_KEY_RE.search(str(key)):
                result[str(key)] = "<redacted>"
            else:
                result[str(key)] = redact_mapping(child)
        return result
    if isinstance(value, list):
        list_result: list[Any] = []
        redact_next = False
        for item in value:
            if redact_next:
                list_result.append("<redacted>")
                redact_next = False
                continue
            list_result.append(redact_mapping(item))
            if isinstance(item, str) and SENSITIVE_FLAG_RE.fullmatch(item):
                redact_next = True
        return list_result
    if isinstance(value, str):
        redacted = SENSITIVE_ARG_RE.sub(r"\g<prefix><redacted>", value)
        return SENSITIVE_SPACED_ARG_RE.sub(r"\g<prefix><redacted>", redacted)
    return value


def _redact_text(value: str) -> str:
    """Mask credential-like tokens in free-form text before returning diagnostics or errors."""
    redacted = redact_mapping(value)
    redacted = SENSITIVE_QUOTED_ARG_LIST_RE.sub(
        lambda match: (
            f"{match.group('prefix')}<redacted>{match.group('value_quote')}"
        ),
        redacted,
    )
    redacted = BEARER_TOKEN_RE.sub("Bearer <redacted>", redacted)
    redacted = HIGH_CONFIDENCE_TOKEN_RE.sub("<redacted>", redacted)
    redacted = URL_USERINFO_PASSWORD_RE.sub(r"\g<prefix><redacted>", redacted)
    redacted = URL_QUERY_RE.sub(r"\g<prefix>?<redacted>", redacted)
    redacted = SENSITIVE_HEADER_VALUE_RE.sub(
        lambda match: f"{match.group('prefix')}<redacted>",
        redacted,
    )
    redacted = SENSITIVE_TEXT_QUOTED_VALUE_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('value_quote')}"
            f"<redacted>{match.group('value_quote')}"
        ),
        redacted,
    )
    return SENSITIVE_TEXT_UNQUOTED_VALUE_RE.sub(
        lambda match: f"{match.group('prefix')}<redacted>",
        redacted,
    )


def _configured_value_variants(value: str) -> set[str]:
    """Build literal and URL-decoded variants of configured secrets so all common renderings can be redacted."""
    variants = {value}
    for serialized in (
        repr(value),
        json.dumps(value),
        json.dumps(value, ensure_ascii=False),
    ):
        variants.add(serialized)
        if (
            len(serialized) >= 2
            and serialized[0] == serialized[-1]
            and serialized[0] in {"'", '"'}
        ):
            variants.add(serialized[1:-1])
    return {variant for variant in variants if variant}


def redact_configured_values(text: str, *maps: dict[str, str]) -> str:
    """Replace configured environment and header values wherever they appear in text."""
    redacted = text
    values = {
        variant
        for mapping in maps
        for value in mapping.values()
        if value
        for variant in _configured_value_variants(value)
    }
    for value in sorted(values, key=lambda item: (-len(item), item)):
        redacted = redacted.replace(value, "<redacted>")
    return redacted


def redact_configured_value_tree(value: Any, *maps: dict[str, str]) -> Any:
    """Apply configured-value and key-based redaction across nested response structures."""
    if isinstance(value, dict):
        return {
            _redact_text(
                redact_configured_values(str(key), *maps)
            ): redact_configured_value_tree(child, *maps)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_configured_value_tree(item, *maps) for item in value]
    if isinstance(value, str):
        return _redact_text(redact_configured_values(value, *maps))
    return value
