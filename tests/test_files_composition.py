import pytest

from workgate.composition.services import (
    build_runtime_services,
    install_runtime_services,
)
from workgate.config.settings import Settings, clear_settings_cache
from workgate.control.search_composition import (
    build_control_tool_catalog,
)
from workgate.executor.search_composition import (
    build_executor_dispatcher_with_search,
)
from workgate.persistence import configure_state_store
from workgate.remote.manager import (
    RemoteManager,
    configure_remote_manager,
)
from workgate.tool_session import configure_tool_session_store
from workgate.tools.registry.files import FileToolRegistry
from workgate.tools.registry.read import ReadToolRegistry


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


def _registry(catalog, registry_type):
    return next(
        registry
        for registry in catalog.registries
        if isinstance(registry, registry_type)
    )


def _tool(registry, name):
    return next(tool for tool in registry._enabled_tools() if tool.name == name)


@pytest.mark.asyncio
async def test_controller_files_and_read_use_explicit_service_without_ambient_fallback(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path, monkeypatch)
    services = _configure_runtime_services(settings)
    (tmp_path / "demo.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    session = services.tool_session_store.create_session(workdir=tmp_path)
    catalog = build_control_tool_catalog(
        settings,
        services.tool_session_store,
        RemoteManager(lambda: settings, state_store=services.state_store),
    )

    file_registry = _registry(catalog, FileToolRegistry)
    read_registry = _registry(catalog, ReadToolRegistry)

    assert all(
        tool.session_admission == "handler"
        for tool in file_registry._enabled_tools()
    )
    assert _tool(read_registry, "read").session_admission == "handler"

    import workgate.ops.files as files_ops
    import workgate.ops.read as read_ops

    def ambient_files_should_not_run():
        raise AssertionError(
            "controller Files fell back to ambient dependencies"
        )

    async def legacy_read_should_not_run(*_args, **_kwargs):
        raise AssertionError("controller Read fell back to legacy read_execute")

    monkeypatch.setattr(
        files_ops, "_compat_files_dependencies", ambient_files_should_not_run
    )
    monkeypatch.setattr(read_ops, "read_execute", legacy_read_should_not_run)

    listed = await _tool(file_registry, "list_files").func(
        session.session_id, ".", False, 10
    )
    read = await _tool(read_registry, "read").func(
        session.session_id, "demo.txt:2"
    )

    assert any(entry.path == "demo.txt" for entry in listed.entries)
    assert read.kind == "file"
    assert read.content.endswith("2:beta")
    assert read.file is not None
    assert read.file.snapshot_id is not None


@pytest.mark.asyncio
async def test_controller_files_remote_wire_uses_owned_manager(
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

    async def fake_call(machine, tool, args, timeout_s=None):
        calls.append((machine, tool, args, timeout_s))
        return {
            "ok": True,
            "data": {
                "limit_count": 10,
                "count": 0,
                "is_truncated": False,
                "entries": [],
            },
        }

    monkeypatch.setattr(manager, "call", fake_call)
    catalog = build_control_tool_catalog(
        settings, services.tool_session_store, manager
    )
    file_registry = _registry(catalog, FileToolRegistry)

    result = await _tool(file_registry, "list_files").func(
        session.session_id, "src", False, 10
    )

    assert result.count == 0
    assert calls == [
        (
            "worker-a",
            "list_files",
            {
                "path": "src",
                "recursive": False,
                "max_entries": 10,
                "session_id": "WORKER01",
            },
            None,
        )
    ]


@pytest.mark.asyncio
async def test_executor_dispatcher_uses_composed_files_and_read_overrides(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path, monkeypatch)
    services = _configure_runtime_services(settings)
    dispatcher = build_executor_dispatcher_with_search(
        settings, services.tool_session_store
    )
    (tmp_path / "demo.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    session = await dispatcher.execute(
        "session_start",
        {
            "workdir": str(tmp_path),
            "target": "local",
            "machine": None,
            "label": None,
        },
    )

    import workgate.ops.files as files_ops
    import workgate.ops.read as read_ops

    async def legacy_files_should_not_run(*_args, **_kwargs):
        raise AssertionError("executor Files fell back to legacy dispatch")

    async def legacy_read_should_not_run(*_args, **_kwargs):
        raise AssertionError("worker Read fell back to legacy read_execute")

    monkeypatch.setattr(
        files_ops, "list_files_dispatch_execute", legacy_files_should_not_run
    )
    monkeypatch.setattr(read_ops, "read_execute", legacy_read_should_not_run)

    listed = await dispatcher.execute(
        "list_files",
        {
            "session_id": session.session_id,
            "path": ".",
            "recursive": False,
            "max_entries": 10,
        },
    )
    read = await dispatcher.execute(
        "read",
        {"session_id": session.session_id, "path": "demo.txt:1-2"},
    )

    assert any(entry.path == "demo.txt" for entry in listed.entries)
    assert read.kind == "file"
    assert read.content.endswith("1:alpha\n2:beta")
    assert read.file is not None
    assert read.file.session_id == session.session_id


@pytest.mark.asyncio
async def test_executor_dispatcher_composed_file_mutations_cover_all_handlers(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path, monkeypatch)
    services = _configure_runtime_services(settings)
    dispatcher = build_executor_dispatcher_with_search(
        settings, services.tool_session_store
    )
    session = await dispatcher.execute(
        "session_start",
        {
            "workdir": str(tmp_path),
            "target": "local",
            "machine": None,
            "label": None,
        },
    )

    await dispatcher.execute(
        "write_file",
        {
            "session_id": session.session_id,
            "path": "edit.txt",
            "content": "alpha\nbeta\n",
            "overwrite": True,
        },
    )
    await dispatcher.execute(
        "edit_lines",
        {
            "session_id": session.session_id,
            "path": "edit.txt",
            "start_line": 2,
            "end_line": 2,
            "replacement": "gamma",
        },
    )
    grounded = await dispatcher.execute(
        "read",
        {"session_id": session.session_id, "path": "edit.txt:1-2"},
    )
    assert grounded.file is not None
    assert grounded.file.snapshot_id is not None
    await dispatcher.execute(
        "hashline_edit",
        {
            "session_id": session.session_id,
            "input": (
                f"[edit.txt#{grounded.file.snapshot_id}]\n"
                "1:alpha\n"
                "2:gamma\n"
                "+delta\n"
                "+epsilon"
            ),
        },
    )
    await dispatcher.execute(
        "delete_file_or_dir",
        {
            "session_id": session.session_id,
            "path": "edit.txt",
            "recursive": False,
        },
    )

    assert not (tmp_path / "edit.txt").exists()


@pytest.mark.asyncio
async def test_bound_file_registry_handlers_delegate_to_injected_service(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path, monkeypatch)
    services = _configure_runtime_services(settings)
    catalog = build_control_tool_catalog(
        settings,
        services.tool_session_store,
        RemoteManager(lambda: settings, state_store=services.state_store),
    )
    registry = _registry(catalog, FileToolRegistry)
    service = registry._files_service
    assert service is not None
    calls = []

    async def fake_write(*args):
        calls.append(("write", args))
        return "write"

    async def fake_edit(*args):
        calls.append(("edit", args))
        return "edit"

    async def fake_hash(*args):
        calls.append(("hash", args))
        return "hash"

    async def fake_delete(*args):
        calls.append(("delete", args))
        return "delete"

    monkeypatch.setattr(service, "write_file", fake_write)
    monkeypatch.setattr(service, "edit_lines", fake_edit)
    monkeypatch.setattr(service, "hashline_edit", fake_hash)
    monkeypatch.setattr(service, "delete_file_or_dir", fake_delete)

    assert (
        await registry._bound_write_file("SESSION1", "a.txt", "alpha", False)
        == "write"
    )
    assert (
        await registry._bound_edit_lines(
            "a.txt", 1, 2, "beta", "SESSION1", "snap"
        )
        == "edit"
    )
    assert await registry._bound_hashline_edit("SESSION1", "payload") == "hash"
    assert (
        await registry._bound_delete_file_or_dir("SESSION1", "a.txt", True)
        == "delete"
    )
    assert [name for name, _args in calls] == [
        "write",
        "edit",
        "hash",
        "delete",
    ]


@pytest.mark.asyncio
async def test_unbound_registries_reject_direct_bound_handler_use() -> None:
    file_registry = FileToolRegistry()

    with pytest.raises(RuntimeError, match="Files service is not bound"):
        file_registry._require_service()

    read_registry = ReadToolRegistry()
    with pytest.raises(RuntimeError, match="Files service is not bound"):
        await read_registry._bound_read("SESSION1", "a.txt")
