import shutil

import pytest

from workgate.composition.services import (
    build_runtime_services,
    install_runtime_services,
)
from workgate.config.settings import Settings, clear_settings_cache
from workgate.control.search_composition import (
    build_control_tool_catalog,
)
from workgate.persistence import configure_state_store
from workgate.remote.manager import (
    RemoteManager,
    configure_remote_manager,
)
from workgate.remote_worker.search_composition import (
    build_worker_dispatcher_with_search,
)
from workgate.schemas.result_models.search import (
    GrepSearchOutput,
)
from workgate.tool_session import configure_tool_session_store
from workgate.tools.registry.search import SearchToolRegistry


@pytest.fixture(autouse=True)
def _restore_global_runtime_services():
    configure_remote_manager(None)
    yield
    configure_remote_manager(None)
    configure_tool_session_store(None)
    configure_state_store(None)
    clear_settings_cache()


def _settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    return Settings()


def _configure_runtime_services(settings: Settings):
    services = build_runtime_services(settings)
    install_runtime_services(services)
    return services


@pytest.mark.asyncio
async def test_controller_catalog_binds_search_service(tmp_path, monkeypatch):
    if not shutil.which("rg"):
        pytest.skip("missing rg")
    settings = _settings(tmp_path, monkeypatch)
    services = _configure_runtime_services(settings)
    (tmp_path / "demo.txt").write_text("needle\n", encoding="utf-8")
    session = services.tool_session_store.create_session(workdir=tmp_path)

    catalog = build_control_tool_catalog(
        settings,
        services.tool_session_store,
        RemoteManager(lambda: settings, state_store=services.state_store),
    )
    registry = next(
        registry
        for registry in catalog.registries
        if isinstance(registry, SearchToolRegistry)
    )
    bound_search = next(
        tool for tool in registry._enabled_tools() if tool.name == "search"
    )

    assert bound_search.session_admission == "handler"
    assert (
        next(
            tool
            for tool in registry._enabled_tools()
            if tool.name == "tree_view"
        ).session_admission
        == "wrapper"
    )
    result = await bound_search.func(
        session.session_id,
        "needle",
        regex=False,
        gitignore=False,
    )
    assert result.ok is True
    assert result.count == 1
    assert result.matches[0].snapshot_id is not None


@pytest.mark.asyncio
async def test_controller_search_remote_wire_uses_owned_manager(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path, monkeypatch)
    services = _configure_runtime_services(settings)
    session = services.tool_session_store.create_session(
        target="remote",
        workdir="/remote/work",
        machine="worker-a",
        worker_session_id="WORKER01",
    )
    manager = RemoteManager(lambda: settings, state_store=services.state_store)
    calls = []
    output = GrepSearchOutput(
        ok=True,
        matches=[],
        displayed_lines=[],
        count=0,
        displayed_count=0,
        context_radius=0,
        skipped=0,
        truncated=False,
        stderr="",
        numbered_content="",
    )

    async def fake_call(machine, tool, args, timeout_s=None):
        calls.append((machine, tool, args, timeout_s))
        return {"ok": True, "data": output.model_dump(mode="json")}

    monkeypatch.setattr(manager, "call", fake_call)
    catalog = build_control_tool_catalog(
        settings,
        services.tool_session_store,
        manager,
    )
    registry = next(
        registry
        for registry in catalog.registries
        if isinstance(registry, SearchToolRegistry)
    )
    bound_search = next(
        tool for tool in registry._enabled_tools() if tool.name == "search"
    )

    result = await bound_search.func(session.session_id, "needle")

    assert result == output
    assert calls == [
        (
            "worker-a",
            "search",
            {
                "pattern": "needle",
                "paths": None,
                "regex": True,
                "case_sensitive": True,
                "max_results": None,
                "skip": 0,
                "gitignore": True,
                "session_id": "WORKER01",
            },
            None,
        )
    ]


@pytest.mark.asyncio
async def test_worker_dispatcher_uses_composed_search_override(
    tmp_path, monkeypatch
):
    if not shutil.which("rg"):
        pytest.skip("missing rg")
    settings = _settings(tmp_path, monkeypatch)
    services = _configure_runtime_services(settings)
    dispatcher = build_worker_dispatcher_with_search(
        settings, services.tool_session_store
    )
    (tmp_path / "demo.txt").write_text("needle\n", encoding="utf-8")
    session = await dispatcher.execute(
        "session_start",
        {
            "workdir": str(tmp_path),
            "target": "local",
            "machine": None,
            "label": None,
        },
    )

    import workgate.ops.search as search_ops

    async def legacy_search_should_not_run(*_args, **_kwargs):
        raise AssertionError("worker Search fell back to legacy search_execute")

    monkeypatch.setattr(
        search_ops, "search_execute", legacy_search_should_not_run
    )
    result = await dispatcher.execute(
        "search",
        {
            "session_id": session.session_id,
            "pattern": "needle",
            "paths": None,
            "regex": False,
            "case_sensitive": True,
            "max_results": None,
            "skip": 0,
            "gitignore": False,
        },
    )

    assert result.ok is True
    assert result.count == 1
    assert result.matches[0].session_id == session.session_id


@pytest.mark.asyncio
async def test_unbound_search_registry_rejects_bound_handler_use():
    registry = SearchToolRegistry()
    with pytest.raises(RuntimeError, match="Search service is not bound"):
        await registry._bound_search("SESSION01", "needle")


@pytest.mark.asyncio
async def test_unbound_search_registry_uses_direct_search_handler(monkeypatch):
    calls = []
    output = GrepSearchOutput(
        ok=True,
        matches=[],
        displayed_lines=[],
        count=0,
        displayed_count=0,
        context_radius=0,
        skipped=0,
        truncated=False,
        stderr="",
        numbered_content="",
    )

    async def fake_search_execute(
        pattern,
        paths,
        cwd,
        regex,
        case_sensitive,
        max_results,
        session_id,
        skip,
        gitignore,
    ):
        calls.append(
            {
                "pattern": pattern,
                "paths": paths,
                "cwd": cwd,
                "regex": regex,
                "case_sensitive": case_sensitive,
                "max_results": max_results,
                "session_id": session_id,
                "skip": skip,
                "gitignore": gitignore,
            }
        )
        return output

    monkeypatch.setattr(
        "workgate.tools.registry.search.search_execute",
        fake_search_execute,
    )
    registry = SearchToolRegistry()
    direct_search = next(
        tool for tool in registry._enabled_tools() if tool.name == "search"
    )

    result = await direct_search.func(
        "SESSION01",
        "needle",
        ["src"],
        False,
        False,
        17,
        3,
        False,
    )

    assert result == output
    assert calls == [
        {
            "pattern": "needle",
            "paths": ["src"],
            "cwd": ".",
            "regex": False,
            "case_sensitive": False,
            "max_results": 17,
            "session_id": "SESSION01",
            "skip": 3,
            "gitignore": False,
        }
    ]
