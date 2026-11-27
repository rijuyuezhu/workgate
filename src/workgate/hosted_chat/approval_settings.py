"""User-owned tool approval preferences, independent of native grants.

Adapted from final v6.0.3 approval_settings.py (0630dbfa0d...).
"""

from collections.abc import Mapping
from typing import Any

from .approval_runtime import approval_policy_decision, ask_user_approval_policy
from .native_tool_kinds import SESSION_FILE_READ_TOOLS, SESSION_SHELL_READ_TOOLS
from .storage import WorkspaceRepository

MODES = ("automatic", "dangerous", "all")
GLOBAL_KEY = "approval.mode"
CHAT_PREFIX = "approval.conversation."


def validate_mode(value: Any, *, inherit: bool = False) -> str:
    if inherit and value == "inherit":
        return value
    if not isinstance(value, str) or value not in MODES:
        raise ValueError("Invalid approval mode")
    return value


def save_mode(
    repo: WorkspaceRepository,
    owner: str,
    value: Any,
    *,
    conversation_id: str | None = None,
) -> str:
    mode = validate_mode(value, inherit=conversation_id is not None)
    if conversation_id is not None:
        if repo.conversation(owner, conversation_id) is None:
            raise KeyError("Conversation not found")
        repo.save_setting(owner, CHAT_PREFIX + conversation_id, mode)
    else:
        repo.save_setting(owner, GLOBAL_KEY, mode)
    return mode


def effective_mode(
    repo: WorkspaceRepository,
    owner: str,
    conversation_id: str,
    agent: Mapping | None = None,
    *,
    parent_mode: str | None = None,
) -> str:
    if repo.conversation(owner, conversation_id) is None:
        raise KeyError("Conversation not found")
    chat = repo.setting(owner, CHAT_PREFIX + conversation_id, "inherit")
    mode = (
        chat if chat in MODES else (agent or {}).get("approval_mode", "inherit")
    )
    if mode not in MODES:
        mode = repo.setting(owner, GLOBAL_KEY, "automatic")
    mode = mode if mode in MODES else "automatic"
    if parent_mode in MODES:
        mode = max(mode, parent_mode, key=MODES.index)
    return mode


def is_dangerous(name: str, arguments: Mapping) -> bool:
    if name in SESSION_FILE_READ_TOOLS | SESSION_SHELL_READ_TOOLS | {
        "read_todos",
        "write_todos",
        "task_plan",
        "session_start",
        "web_search",
        "web_fetch",
        "ask_user_question",
        "subagent",
    }:
        return False
    if name == "task":
        return arguments.get("action") not in (
            "create",
            "get",
            "report",
            "finish",
        )
    if name == "job":
        return (
            arguments.get("cancel") is not None
            or arguments.get("retry") is not None
        )
    if name == "browser_session":
        return arguments.get("action") != "list"
    if name == "bash":
        return (
            arguments.get("command") not in ("ls", "pwd")
            or any(
                arguments.get(flag, False) is not False
                for flag in ("async_", "pty", "persist_on_timeout")
            )
            or bool(arguments.get("env"))
        )
    # Native Session close may terminate background work; it is not the
    # upstream logical session-finish metadata operation.
    return True


def call_decision(
    mode: str, legacy_policy: Mapping | None, name: str, arguments: Mapping
) -> dict | None:
    validate_mode(mode)
    policy = (
        ask_user_approval_policy(legacy_policy)
        if name == "ask_user_question"
        else legacy_policy
    )
    legacy = approval_policy_decision(policy, name)
    if legacy and legacy["decision"] in {"deny", "ask"}:
        return legacy
    if name != "ask_user_question" and (
        mode == "all" or mode == "dangerous" and is_dangerous(name, arguments)
    ):
        return {"decision": "ask"}
    return legacy
