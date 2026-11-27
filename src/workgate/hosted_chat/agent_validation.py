"""JSON-facing port of LibreChat agents/validation.ts (e8f3be08).

Source: upstream v6.0.3, webui/agent_validation.py
(b500411155f878f295a682697cf9a0211532dbab). See LICENSE.upstream.
This unit validates core authoring only. Tool selection never grants access.
"""

import contextlib
import copy
import math
from typing import Any

_NUMERIC = frozenset(
    [
        "temperature",
        "top_p",
        "topP",
        "top_k",
        "topK",
        "frequency_penalty",
        "frequencyPenalty",
        "presence_penalty",
        "presencePenalty",
        "max_tokens",
        "maxTokens",
        "max_output_tokens",
        "maxOutputTokens",
        "max_context_tokens",
        "maxContextTokens",
        "fileTokenLimit",
        "thinking_budget",
        "thinkingBudget",
    ]
)


def _require(value: Any, kind: type, path: str) -> Any:
    if type(value) is not kind:
        raise ValueError(f"{path} must be {kind.__name__}")
    return value


def _strings(value: Any, path: str) -> list[str]:
    return [_require(item, str, path) for item in _require(value, list, path)]


def clean_edge(value: Any) -> dict:
    """Validate saved foreground edge fields from the pinned authoring source."""
    edge = _require(value, dict, "edge")
    result = {}
    for key in ("from", "to"):
        if key not in edge:
            raise ValueError(f"edge.{key} is required")
        item = edge[key]
        result[key] = (
            item if type(item) is str else _strings(item, "edge." + key)
        )
        if type(item) is list and not item:
            raise ValueError("Graph edge endpoints cannot be empty arrays")
    for key in ("description", "prompt", "promptKey"):
        if key in edge:
            value = _require(edge[key], str, "edge." + key)
            if value:
                result[key] = value
    for key, choices in (
        ("edgeType", ("handoff", "direct")),
        ("handoffScope", ("turn", "conversation")),
    ):
        if key in edge:
            value = edge[key]
            if type(value) is not str or value not in choices:
                raise ValueError(f"Invalid {key}")
            result[key] = value
    if "excludeResults" in edge:
        result["excludeResults"] = _require(
            edge["excludeResults"], bool, "excludeResults"
        )
    return result


def replace_graph_self(config: dict, previous: str, next_id: str) -> dict:
    """Expand source origin placeholders or rebind a duplicated graph's self."""
    result = copy.deepcopy(config)

    def replace(value):
        if isinstance(value, list):
            return [next_id if item == previous else item for item in value]
        return next_id if value == previous else value

    for edge in result.get("edges", []):
        edge["from"] = replace(edge["from"])
        if previous:
            edge["to"] = replace(edge["to"])
    if previous:
        # Duplication changes identity, including explicit self entry/result
        # and cycles that point back to the original Agent.
        for key in ("graphAgentIds", "entryAgentId", "resultAgentId"):
            if key in result:
                result[key] = replace(result[key])
    return result


def clean_agent_config(payload: Any, *, create: bool = False) -> dict:
    obj = _require(payload, dict, "Agent configuration")
    result = {}
    # Only the authoring fields integrated in H1 are accepted here. The later
    # feature units will extend this allowlist with their own validators.
    nullable_strings = {
        "name",
        "description",
        "instructions",
        "model",
        "entryAgentId",
        "resultAgentId",
    }
    strings = {"provider", "category"}
    arrays = {"tools", "conversation_starters", "graphAgentIds"}
    for key in nullable_strings | strings | arrays:
        if key not in obj:
            continue
        value = obj[key]
        if value is None and key in nullable_strings:
            result[key] = None
        elif key in nullable_strings | strings:
            result[key] = _require(value, str, key)
        else:
            result[key] = _strings(value, key)
    if "provider" in result and not result["provider"].strip():
        raise ValueError("Agent provider is required")
    if "approval_mode" in obj:
        from .approval_settings import validate_mode

        result["approval_mode"] = validate_mode(
            obj["approval_mode"], inherit=True
        )
    if "model_parameters" in obj:
        result["model_parameters"] = copy.deepcopy(
            _require(obj["model_parameters"], dict, "model_parameters")
        )
    if "edges" in obj:
        result["edges"] = [
            clean_edge(edge) for edge in _require(obj["edges"], list, "edges")
        ]
    if "maxHandoffs" in obj:
        limit = obj["maxHandoffs"]
        if type(limit) is not int or not 0 <= limit <= 2**53 - 1:
            raise ValueError("maxHandoffs must be a nonnegative safe integer")
        result["maxHandoffs"] = limit
    if create:
        if "provider" not in result or "model" not in result:
            raise ValueError("Agent provider and model are required")
        result.setdefault("tools", [])
    return result


def sanitize_model_parameters(params: dict) -> dict:
    """Port of agents/parameters.ts; numeric placeholders never reach models."""
    result = {}
    for key, value in params.items():
        if key not in _NUMERIC:
            result[key] = value
            continue
        number = None
        if type(value) in (int, float):
            number = value
        elif type(value) is str and value.strip():
            text = value.strip()
            with contextlib.suppress(ValueError):
                number = (
                    int(text, 0)
                    if text.lower().startswith(("0x", "0o", "0b"))
                    else float(text)
                )
        with contextlib.suppress(OverflowError):
            if number is not None and math.isfinite(number):
                result[key] = number
    return result
