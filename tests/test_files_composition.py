import pytest

from workgate.config.settings import Settings, clear_settings_cache
from workgate.executor.search_composition import (
    build_executor_dispatcher_with_search,
)
from workgate.executor.services import build_runtime_services
from workgate.tools.registry.files import FileToolRegistry
from workgate.tools.registry.read import ReadToolRegistry


def _settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    return Settings()


def _session_id(index: int) -> str:
    return f"sess_{index:022d}"


@pytest.mark.asyncio
async def test_composed_files_cover_read_write_edit_hashline_and_delete(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path, monkeypatch)
    services = build_runtime_services(settings)
    store = services.tool_session_store
    session = store.create_session(session_id=_session_id(1), workdir=tmp_path)
    dispatcher = build_executor_dispatcher_with_search(settings, store)

    written = await dispatcher.execute(
        "write_file",
        {
            "session_id": session.session_id,
            "path": "demo.txt",
            "content": "one\ntwo\nthree\n",
        },
    )
    read = await dispatcher.execute(
        "read", {"session_id": session.session_id, "path": "demo.txt"}
    )
    edited = await dispatcher.execute(
        "edit_lines",
        {
            "session_id": session.session_id,
            "path": "demo.txt",
            "start_line": 2,
            "end_line": 2,
            "replacement": "TWO",
            "snapshot_id": read.file.snapshot_id,
        },
    )
    fresh = await dispatcher.execute(
        "read", {"session_id": session.session_id, "path": "demo.txt"}
    )
    hashline = await dispatcher.execute(
        "hashline_edit",
        {
            "session_id": session.session_id,
            "input": (f"[demo.txt#{fresh.file.snapshot_id}]\n3:three\n+THREE"),
        },
    )
    listed = await dispatcher.execute(
        "list_files", {"session_id": session.session_id, "path": "."}
    )
    deleted = await dispatcher.execute(
        "delete_file_or_dir",
        {"session_id": session.session_id, "path": "demo.txt"},
    )

    assert written.path == "demo.txt"
    assert "TWO" in edited.context.content
    assert "THREE" in hashline.context.content
    assert any(entry.path == "demo.txt" for entry in listed.entries)
    assert deleted.deleted == "file"
    assert not (tmp_path / "demo.txt").exists()


@pytest.mark.asyncio
async def test_composed_files_resolve_fresh_session_workdir_each_call(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path, monkeypatch)
    services = build_runtime_services(settings)
    store = services.tool_session_store
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    session = store.create_session(session_id=_session_id(2), workdir=first)
    dispatcher = build_executor_dispatcher_with_search(settings, store)

    await dispatcher.execute(
        "write_file",
        {
            "session_id": session.session_id,
            "path": "value.txt",
            "content": "first",
        },
    )
    store.change_session_workdir(session.session_id, second)
    await dispatcher.execute(
        "write_file",
        {
            "session_id": session.session_id,
            "path": "value.txt",
            "content": "second",
        },
    )

    assert (first / "value.txt").read_text() == "first"
    assert (second / "value.txt").read_text() == "second"


@pytest.mark.asyncio
async def test_file_registry_is_declaration_only_and_fails_closed() -> None:
    tools = {tool.name: tool for tool in FileToolRegistry()._enabled_tools()}

    with pytest.raises(
        RuntimeError, match="list_files requires control routing"
    ):
        await tools["list_files"].func(_session_id(1))
    with pytest.raises(
        RuntimeError, match="write_file requires control routing"
    ):
        await tools["write_file"].func(_session_id(1), "a.txt", "x")
    with pytest.raises(
        RuntimeError, match="edit_lines requires control routing"
    ):
        await tools["edit_lines"].func("a.txt", 1, 1, "x", _session_id(1))
    with pytest.raises(
        RuntimeError, match="hashline_edit requires control routing"
    ):
        await tools["hashline_edit"].func(_session_id(1), "input")
    with pytest.raises(
        RuntimeError, match="delete_file_or_dir requires control routing"
    ):
        await tools["delete_file_or_dir"].func(_session_id(1), "a.txt")


@pytest.mark.asyncio
async def test_read_registry_is_declaration_only_and_fails_closed() -> None:
    tool = next(iter(ReadToolRegistry()._enabled_tools()))
    with pytest.raises(RuntimeError, match="read requires control routing"):
        await tool.func(_session_id(1), "a.txt")
