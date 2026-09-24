#!/usr/bin/env python3
"""Generate the dependency-light hosted MCP tool manifest from canonical tools."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
from mcp.types import LATEST_PROTOCOL_VERSION

from workgate.config.settings import Settings
from workgate.control.mcp.app import build_mcp
from workgate.tools.catalog import build_tool_catalog

HOSTED_TOOL_NAMES = (
    "list_agent_skills",
    "activate_agent_skill",
    "read_agent_skill_file",
    "list_files",
    "write_file",
    "edit_lines",
    "hashline_edit",
    "delete_file_or_dir",
    "apply_patch",
    "read",
    "tree_view",
    "glob_search",
    "search",
    "secret_scan",
    "session_start",
    "session_change_cwd",
    "session_end",
    "bash",
    "run_python_code",
    "send_persistent_shell_input",
    "resize_persistent_shell",
    "read_persistent_shell_output",
    "kill_persistent_shell",
    "list_persistent_shells",
    "workspace_search",
    "fetch",
)

_SUPPORTED_INPUT_SCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "anyOf",
        "default",
        "description",
        "items",
        "maxLength",
        "maximum",
        "minLength",
        "minimum",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
    }
)


def _validate_input_schema(schema: object, *, path: str) -> None:
    """Fail generation if hosted runtime validation would ignore a constraint."""
    if not isinstance(schema, dict):
        raise RuntimeError(f"invalid canonical MCP schema at {path}")
    unsupported = sorted(set(schema) - _SUPPORTED_INPUT_SCHEMA_KEYS)
    if unsupported:
        names = ", ".join(unsupported)
        raise RuntimeError(
            f"unsupported hosted MCP schema keyword at {path}: {names}"
        )
    variants = schema.get("anyOf")
    if isinstance(variants, list):
        for index, variant in enumerate(variants):
            _validate_input_schema(variant, path=f"{path}.anyOf[{index}]")
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, child in properties.items():
            _validate_input_schema(child, path=f"{path}.properties.{name}")
    items = schema.get("items")
    if isinstance(items, dict):
        _validate_input_schema(items, path=f"{path}.items")
    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        _validate_input_schema(additional, path=f"{path}.additionalProperties")


async def build_manifest() -> list[dict[str, object]]:
    mcp = build_mcp(tool_catalog=build_tool_catalog(Settings()))
    by_name = {tool.name: tool for tool in await mcp.list_tools()}
    missing = sorted(set(HOSTED_TOOL_NAMES) - by_name.keys())
    if missing:
        raise RuntimeError(
            f"missing canonical hosted tools: {', '.join(missing)}"
        )
    manifest: list[dict[str, object]] = []
    for name in HOSTED_TOOL_NAMES:
        tool = by_name[name]
        _validate_input_schema(tool.inputSchema, path=f"{name}.inputSchema")
        row: dict[str, object] = {
            "name": tool.name,
            "description": tool.description or "",
            "inputSchema": tool.inputSchema,
        }
        if tool.outputSchema is not None:
            row["outputSchema"] = tool.outputSchema
        if tool.annotations is not None:
            row["annotations"] = tool.annotations.model_dump(
                mode="json", exclude_none=True
            )
        manifest.append(row)
    return manifest


def render(manifest: list[dict[str, object]]) -> str:
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2)
    versions = repr(tuple(SUPPORTED_PROTOCOL_VERSIONS))
    latest = repr(LATEST_PROTOCOL_VERSION)
    return (
        '"""Generated hosted MCP manifest. Do not edit by hand."""\n\n'
        "from __future__ import annotations\n\n"
        "import json\n\n"
        f"LATEST_MCP_PROTOCOL_VERSION = {latest}\n"
        f"SUPPORTED_MCP_PROTOCOL_VERSIONS = frozenset({versions})\n\n"
        "HOSTED_TOOL_MANIFEST: tuple[dict[str, object], ...] = tuple(\n"
        "    json.loads(\n"
        "        r'''\n" + payload + "\n'''\n"
        "    )\n"
        ")\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("src/workgate/hosted/_tool_manifest.py"),
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = render(asyncio.run(build_manifest()))
    if args.check:
        current = args.output.read_text(encoding="utf-8")
        if current != rendered:
            raise SystemExit("hosted tool manifest is stale")
        return
    args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
