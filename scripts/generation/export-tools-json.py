#!/usr/bin/env python3
"""Export MCP reference data as JSON."""

import argparse
import asyncio
import inspect
import json
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from workgate.config.settings import clear_settings_cache
from workgate.control.mcp.app import build_mcp
from workgate.control.mcp.instructions import SERVER_INSTRUCTIONS
from workgate.tools.catalog import build_tool_catalog
from workgate.tools.declarative import DeclarativeToolRegistry
from workgate.utils.serialization import to_jsonable


def _tool_to_jsonable_dict(tool: Any) -> dict[str, Any]:
    """Return a JSON-serializable representation of one MCP tool."""
    data = to_jsonable(tool, exclude_none=True)
    if isinstance(data, dict):
        return data
    if hasattr(tool, "dict"):
        return tool.dict(exclude_none=True)
    raise TypeError(f"Unsupported tool object type: {type(tool)!r}")


async def export_tools() -> list[dict[str, Any]]:
    """Build the MCP app and return the tools it exposes to clients."""
    with tempfile.TemporaryDirectory(prefix="workgate-tools-") as tmp:
        tmp_root = Path(tmp)
        os.environ.setdefault(
            "WORKGATE_WORKSPACE_ROOT", str(tmp_root / "workspace")
        )
        os.environ.setdefault("WORKGATE_STATE_DIR", str(tmp_root / "state"))
        clear_settings_cache()
        tools = await build_mcp().list_tools()
    return [_tool_to_jsonable_dict(tool) for tool in tools]


def _sorted_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return tools in stable name order."""
    return sorted(tools, key=lambda tool: str(tool.get("name", "")))


def _display_name(name: str) -> str:
    """Return a readable heading from a registry name."""
    special = {"workspace_connector": "Read-only connector tools"}
    if name in special:
        return special[name]
    return name.replace("_", " ").title()


def _registry_group_map() -> dict[str, str]:
    """Return tool-name to registry-name mapping from declarative registries."""
    mapping: dict[str, str] = {}
    for registry in build_tool_catalog().registries:
        if isinstance(registry, DeclarativeToolRegistry):
            for tool in registry.tools:
                mapping[tool.name] = registry.name
    return mapping


def _registry_docs() -> dict[str, str]:
    """Return registry-name to normalized registry docstring mapping."""
    docs: dict[str, str] = {}
    for registry in build_tool_catalog().registries:
        doc = inspect.getdoc(registry.__class__) or ""
        docs[registry.name] = " ".join(inspect.cleandoc(doc).split())
    return docs


def _group_name(tool_name: str, registry_map: dict[str, str]) -> str:
    """Return the registry group name for a generated tool."""
    if tool_name in registry_map:
        return registry_map[tool_name]
    if tool_name.startswith("remote_") or "_remote_" in tool_name:
        return "remote"
    if tool_name.startswith("agent_") or tool_name.startswith("list_agent_"):
        return "agent_bridge"
    if tool_name in {"activate_agent_skill", "call_agent_mcp_tool"}:
        return "agent_bridge"
    return "other"


def _schema_type(schema: dict[str, Any]) -> str:
    """Return a compact type label for a JSON schema fragment."""
    if "enum" in schema:
        return " / ".join(str(item) for item in schema["enum"])
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return " / ".join(str(item) for item in schema_type)
    if schema_type:
        return str(schema_type)
    variants = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(variants, list):
        return " / ".join(
            _schema_type(item) for item in variants if isinstance(item, dict)
        )
    if "items" in schema:
        return "array"
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", maxsplit=1)[-1]
    return "object"


def _parameter_cell(tool: dict[str, Any]) -> dict[str, Any] | str:
    """Return generated parameter cell data for one tool."""
    schema = tool.get("inputSchema") or {}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if not properties:
        return "none"
    items = []
    for name in sorted(properties):
        prop = properties[name]
        type_label = _schema_type(prop) if isinstance(prop, dict) else "value"
        status = "required" if name in required else "optional"
        description = (
            prop.get("description") if isinstance(prop, dict) else None
        )
        items.append(
            {
                "head": {"code": name},
                "note": f"({type_label}, {status})",
                "description": description,
            }
        )
    return {"items": items}


def _tool_sections(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return table sections generated from tool metadata and registries."""
    registry_map = _registry_group_map()
    registry_docs = _registry_docs()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for tool in _sorted_tools(tools):
        grouped[_group_name(str(tool.get("name", "")), registry_map)].append(
            tool
        )

    registry_order = [
        registry.name for registry in build_tool_catalog().registries
    ]
    ordered_groups = [*registry_order, "other"]
    sections = [
        {
            "kind": "table",
            "heading": _display_name(group),
            "body": registry_docs.get(group, ""),
            "headers": ["Tool", "Parameters", "Description"],
            "rows": [
                [
                    {"code": tool.get("name", "")},
                    _parameter_cell(tool),
                    tool.get("description", ""),
                ]
                for tool in grouped[group]
            ],
        }
        for group in ordered_groups
        if grouped.get(group)
    ]
    return sections


def _wrapped_payload(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a stable wrapped JSON payload with render sections."""
    sorted_tools = _sorted_tools(tools)
    return {
        "count": len(sorted_tools),
        "tools": sorted_tools,
        "sections": [
            {
                "kind": "paragraph",
                "body": [
                    "Generated tool count: ",
                    {"code": str(len(sorted_tools))},
                    ".",
                ],
            },
            *_tool_sections(sorted_tools),
        ],
    }


def _instructions_payload() -> dict[str, Any]:
    """Return MCP server instructions reference data."""
    return {
        "instructions": SERVER_INSTRUCTIONS,
        "sections": [
            {
                "kind": "markdown",
                "markdown": SERVER_INSTRUCTIONS,
            }
        ],
    }


def write_json(data: Any, output: Path | None, *, check: bool) -> bool:
    """Write JSON data to stdout or a file, or check that a file is current."""
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is None:
        if check:
            raise ValueError("--check requires --output")
        sys.stdout.write(text)
        return True
    if check:
        return output.exists() and output.read_text() == text
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text)
    return True


async def async_main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Write tools JSON to this file instead of stdout.",
    )
    parser.add_argument(
        "--wrapped",
        action="store_true",
        help="Wrap the tool array in an object with a count field.",
    )
    parser.add_argument(
        "--instructions-output",
        type=Path,
        help="Also write MCP server instructions reference JSON to this file.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if generated JSON files are stale.",
    )
    args = parser.parse_args()

    tools = await export_tools()
    payload: Any = _wrapped_payload(tools) if args.wrapped else tools
    ok = write_json(payload, args.output, check=args.check)
    if args.instructions_output is not None:
        ok = (
            write_json(
                _instructions_payload(),
                args.instructions_output,
                check=args.check,
            )
            and ok
        )
    if not ok:
        print("generated MCP reference JSON is out of date", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
