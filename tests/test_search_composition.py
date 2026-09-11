import shutil

import pytest

from workgate.config.settings import Settings, clear_settings_cache
from workgate.executor.config import resolve_executor_config
from workgate.executor.search_composition import (
    build_executor_dispatcher_with_search,
)
from workgate.executor.services import build_runtime_services
from workgate.tools.registry.search import SearchToolRegistry
from workgate.tools.registry.workspace_connector import (
    WorkspaceConnectorToolRegistry,
)


def _settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    return Settings()


def _session_id(index: int) -> str:
    return f"sess_{index:022d}"


@pytest.mark.asyncio
async def test_executor_dispatcher_uses_composed_search_discovery_and_connector(
    tmp_path, monkeypatch
):
    if not shutil.which("rg"):
        pytest.skip("missing rg")
    settings = _settings(tmp_path, monkeypatch)
    config = resolve_executor_config(settings)
    services = build_runtime_services(config)
    store = services.tool_session_store
    session = store.create_session(session_id=_session_id(1), workdir=tmp_path)
    dispatcher = build_executor_dispatcher_with_search(config, store)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.txt").write_text(
        "alpha\nneedle here\ngamma\n", encoding="utf-8"
    )

    search = await dispatcher.execute(
        "search",
        {
            "session_id": session.session_id,
            "pattern": "needle",
            "paths": "src",
            "regex": False,
            "gitignore": False,
        },
    )
    glob = await dispatcher.execute(
        "glob_search",
        {
            "session_id": session.session_id,
            "pattern": "*.txt",
            "cwd": ".",
        },
    )
    tree = await dispatcher.execute(
        "tree_view",
        {"session_id": session.session_id, "cwd": ".", "depth": 2},
    )
    connector = await dispatcher.execute(
        "workspace_search",
        {"session_id": session.session_id, "query": "needle"},
    )
    fetched = await dispatcher.execute(
        "fetch",
        {"session_id": session.session_id, "id": "src/demo.txt"},
    )

    assert search.ok is True
    assert [match.path for match in search.matches] == ["src/demo.txt"]
    assert glob.paths == ["src/demo.txt"]
    assert "src/" in tree.entries
    assert "  demo.txt" in tree.entries
    assert [result.id for result in connector.results] == ["src/demo.txt"]
    assert fetched.id == "src/demo.txt"
    assert fetched.text == "alpha\nneedle here\ngamma\n"


@pytest.mark.asyncio
async def test_composed_search_resolves_each_operation_from_session_workdir(
    tmp_path, monkeypatch
):
    if not shutil.which("rg"):
        pytest.skip("missing rg")
    settings = _settings(tmp_path, monkeypatch)
    config = resolve_executor_config(settings)
    services = build_runtime_services(config)
    store = services.tool_session_store
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "one.txt").write_text("needle first\n", encoding="utf-8")
    (second / "two.txt").write_text("needle second\n", encoding="utf-8")
    session = store.create_session(session_id=_session_id(2), workdir=first)
    dispatcher = build_executor_dispatcher_with_search(config, store)

    first_result = await dispatcher.execute(
        "workspace_search",
        {"session_id": session.session_id, "query": "needle"},
    )
    store.change_session_workdir(session.session_id, second)
    second_result = await dispatcher.execute(
        "workspace_search",
        {"session_id": session.session_id, "query": "needle"},
    )

    assert [item.id for item in first_result.results] == ["first/one.txt"]
    assert [item.id for item in second_result.results] == ["second/two.txt"]


@pytest.mark.asyncio
async def test_search_registry_is_declaration_only_and_fails_closed() -> None:
    registry = SearchToolRegistry()
    tools = {tool.name: tool for tool in registry._enabled_tools()}

    with pytest.raises(
        RuntimeError, match="tree_view requires control routing"
    ):
        await tools["tree_view"].func(_session_id(1))
    with pytest.raises(
        RuntimeError, match="glob_search requires control routing"
    ):
        await tools["glob_search"].func(_session_id(1), "*.py")
    with pytest.raises(RuntimeError, match="search requires control routing"):
        await tools["search"].func(_session_id(1), "needle")


@pytest.mark.asyncio
async def test_workspace_connector_registry_is_declaration_only() -> None:
    registry = WorkspaceConnectorToolRegistry()
    tools = {tool.name: tool for tool in registry._enabled_tools()}

    with pytest.raises(
        RuntimeError, match="workspace_search requires control routing"
    ):
        await tools["workspace_search"].func(_session_id(1), "needle")
    with pytest.raises(RuntimeError, match="fetch requires control routing"):
        await tools["fetch"].func(_session_id(1), "demo.txt")
