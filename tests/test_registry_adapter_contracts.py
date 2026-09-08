from __future__ import annotations

from typing import Any

import pytest

import workgate.tools.registry.agent as agent_registry
import workgate.tools.registry.downloads as downloads_registry
import workgate.tools.registry.image as image_registry
import workgate.tools.registry.jobs as jobs_registry
import workgate.tools.registry.patch as patch_registry
import workgate.tools.registry.search as search_registry
import workgate.tools.registry.session as session_registry
import workgate.tools.registry.shell as shell_registry
import workgate.tools.registry.todo as todo_registry
import workgate.tools.registry.workspace_connector as connector_registry


@pytest.mark.asyncio
async def test_session_shell_and_job_registry_adapters_preserve_arguments(
    monkeypatch,
):
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def async_call(name: str, *args: Any, **kwargs: Any) -> str:
        calls.append((name, args, kwargs))
        return name

    monkeypatch.setattr(
        session_registry,
        "session_change_cwd_execute",
        lambda *args, **kwargs: async_call(
            "session_change_cwd", *args, **kwargs
        ),
    )
    monkeypatch.setattr(
        session_registry,
        "session_copy_execute",
        lambda *args, **kwargs: async_call("session_copy", *args, **kwargs),
    )
    monkeypatch.setattr(
        jobs_registry,
        "job_execute",
        lambda *args, **kwargs: async_call("job", *args, **kwargs),
    )
    monkeypatch.setattr(
        shell_registry,
        "send_persistent_shell_input_execute",
        lambda *args, **kwargs: async_call("send", *args, **kwargs),
    )
    monkeypatch.setattr(
        shell_registry,
        "resize_persistent_shell_execute",
        lambda *args, **kwargs: async_call("resize", *args, **kwargs),
    )
    monkeypatch.setattr(
        shell_registry,
        "read_persistent_shell_output_execute",
        lambda *args, **kwargs: async_call("read_shell", *args, **kwargs),
    )
    monkeypatch.setattr(
        shell_registry,
        "kill_persistent_shell_execute",
        lambda *args, **kwargs: async_call("kill", *args, **kwargs),
    )
    monkeypatch.setattr(
        shell_registry,
        "list_persistent_shells_execute",
        lambda *args, **kwargs: async_call("list_shells", *args, **kwargs),
    )

    assert (
        await session_registry.session_change_cwd.func("sess_a", "subdir")
        == "session_change_cwd"
    )
    assert (
        await session_registry.session_copy.func(
            "sess_a",
            "src",
            "sess_b",
            "dst",
            "dir",
            False,
            4096,
            True,
        )
        == "session_copy"
    )
    assert (
        await jobs_registry.job.func(
            "sess_a",
            True,
            ["job_poll"],
            None,
            None,
            False,
            77,
        )
        == "job"
    )
    assert (
        await shell_registry.send_persistent_shell_input.func(
            "sess_a", "shell_a", "hello", False
        )
        == "send"
    )
    assert (
        await shell_registry.resize_persistent_shell.func(
            "sess_a", "shell_a", 90, 30
        )
        == "resize"
    )
    assert (
        await shell_registry.read_persistent_shell_output.func(
            "sess_a", "shell_a", 42
        )
        == "read_shell"
    )
    assert (
        await shell_registry.kill_persistent_shell.func("sess_a", "shell_a")
        == "kill"
    )
    assert (
        await shell_registry.list_persistent_shells.func("sess_a")
        == "list_shells"
    )

    assert calls == [
        ("session_change_cwd", ("sess_a", "subdir"), {}),
        (
            "session_copy",
            ("sess_a", "src", "sess_b", "dst", "dir", False, 4096, True),
            {},
        ),
        ("job", ("sess_a", True, ["job_poll"], None, None, False, 77), {}),
        ("send", ("shell_a", "hello", False), {}),
        ("resize", ("shell_a", 90, 30), {}),
        ("read_shell", ("shell_a", 42), {}),
        ("kill", ("shell_a",), {}),
        ("list_shells", (), {}),
    ]


@pytest.mark.asyncio
async def test_workspace_registry_adapters_preserve_arguments(monkeypatch):
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def async_call(name: str, *args: Any, **kwargs: Any) -> str:
        calls.append((name, args, kwargs))
        return name

    monkeypatch.setattr(
        image_registry,
        "view_image_dispatch_execute",
        lambda *args, **kwargs: async_call("view_image", *args, **kwargs),
    )
    monkeypatch.setattr(
        patch_registry,
        "apply_patch_dispatch_execute",
        lambda *args, **kwargs: async_call("apply_patch", *args, **kwargs),
    )
    monkeypatch.setattr(
        search_registry,
        "tree_view_execute",
        lambda *args, **kwargs: async_call("tree", *args, **kwargs),
    )
    monkeypatch.setattr(
        search_registry,
        "glob_search_execute",
        lambda *args, **kwargs: async_call("glob", *args, **kwargs),
    )
    monkeypatch.setattr(
        connector_registry,
        "search_execute",
        lambda *args, **kwargs: async_call("connector_search", *args, **kwargs),
    )
    monkeypatch.setattr(
        connector_registry,
        "fetch_execute",
        lambda *args, **kwargs: async_call("connector_fetch", *args, **kwargs),
    )

    assert (
        await image_registry.view_image.func("sess_a", "image.png")
        == "view_image"
    )
    assert (
        await patch_registry.apply_patch.func("sess_a", "diff", "src")
        == "apply_patch"
    )
    assert (
        await search_registry.tree_view.func("sess_a", "src", 4, 123) == "tree"
    )
    assert (
        await search_registry.glob_search.func("sess_a", "*.py", "src", 321)
        == "glob"
    )
    assert (
        await connector_registry.workspace_search.func("sess_a", "needle")
        == "connector_search"
    )
    assert (
        await connector_registry.fetch.func("sess_a", "README.md")
        == "connector_fetch"
    )

    assert calls == [
        ("view_image", ("image.png", "sess_a"), {}),
        ("apply_patch", ("diff", "src", "sess_a"), {}),
        ("tree", ("sess_a", "src", 4, 123), {}),
        ("glob", ("sess_a", "*.py", "src", 321), {}),
        ("connector_search", ("needle",), {}),
        ("connector_fetch", ("README.md",), {}),
    ]


@pytest.mark.asyncio
async def test_threaded_and_agent_registry_adapters_preserve_arguments(
    monkeypatch,
):
    calls: list[tuple[str, tuple[Any, ...]]] = []

    def revoke(token: str, session_id: str) -> str:
        calls.append(("revoke", (token, session_id)))
        return "revoked"

    def write_todos(
        todos: list[dict[str, Any]], session_id: str, revision: int | None
    ) -> str:
        calls.append(("write_todos", (todos, session_id, revision)))
        return "written"

    async def async_call(name: str, *args: Any) -> str:
        calls.append((name, args))
        return name

    monkeypatch.setattr(downloads_registry, "revoke_file_link_execute", revoke)
    monkeypatch.setattr(todo_registry, "write_todos_execute", write_todos)
    monkeypatch.setattr(
        agent_registry,
        "list_agent_skills_dispatch_execute",
        lambda *args: async_call("list_skills", *args),
    )
    monkeypatch.setattr(
        agent_registry,
        "activate_agent_skill_dispatch_execute",
        lambda *args: async_call("activate_skill", *args),
    )
    monkeypatch.setattr(
        agent_registry,
        "read_agent_skill_file_dispatch_execute",
        lambda *args: async_call("read_skill_file", *args),
    )

    assert (
        await downloads_registry.revoke_file_link.func("sess_a", "token_a")
        == "revoked"
    )
    todos = [{"content": "ship it", "status": "pending"}]
    assert await todo_registry.write_todos.func("sess_a", todos, 9) == "written"
    assert (
        await agent_registry.list_agent_skills.func("sess_a") == "list_skills"
    )
    assert (
        await agent_registry.activate_agent_skill.func("skill-a", "sess_a")
        == "activate_skill"
    )
    assert (
        await agent_registry.read_agent_skill_file.func(
            "skill-a", "notes.md", "sess_a"
        )
        == "read_skill_file"
    )

    assert calls == [
        ("revoke", ("token_a", "sess_a")),
        ("write_todos", (todos, "sess_a", 9)),
        ("list_skills", ("sess_a",)),
        ("activate_skill", ("skill-a", "sess_a")),
        ("read_skill_file", ("skill-a", "notes.md", "sess_a")),
    ]
