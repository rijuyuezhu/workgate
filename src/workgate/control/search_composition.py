"""Control-side composition for migrated domain services."""

from ..config.settings import Settings
from ..ops.files import files_config_from_settings
from ..ops.files_service import FilesService, RemoteFilesClient
from ..ops.search.composition import build_search_service
from ..ops.search.service import RemoteSearchClient
from ..ops.utils.remote_session import call_remote_session_tool
from ..remote.manager import RemoteManager
from ..tool_session.store import ToolSessionStore
from ..tools.catalog import ToolCatalog, build_tool_catalog
from ..tools.registry.files import FileToolRegistry
from ..tools.registry.read import ReadToolRegistry
from ..tools.registry.search import SearchToolRegistry


def build_control_tool_catalog(
    settings: Settings,
    store: ToolSessionStore,
    remote_manager: RemoteManager,
) -> ToolCatalog:
    """Bind migrated control domains through the Phase 2 catalog seam."""

    async def call_remote_search(binding, tool, args):
        return await call_remote_session_tool(
            binding,
            tool,
            args,
            call_worker=remote_manager.call,
        )

    search_service = build_search_service(
        settings,
        store,
        remote=RemoteSearchClient(call=call_remote_search),
    )

    async def call_remote_files(binding, tool, args):
        return await call_remote_session_tool(
            binding,
            tool,
            args,
            call_worker=remote_manager.call,
        )

    files_service = FilesService(
        files_config_from_settings(settings),
        store,
        remote=RemoteFilesClient(call=call_remote_files),
    )

    def files_registry(configured_settings: Settings | None):
        return FileToolRegistry(
            configured_settings,
            files_service=files_service,
        )

    def read_registry(configured_settings: Settings | None):
        return ReadToolRegistry(
            configured_settings,
            files_service=files_service,
        )

    def search_registry(configured_settings: Settings | None):
        return SearchToolRegistry(
            configured_settings,
            search_service=search_service,
        )

    return build_tool_catalog(
        settings,
        factory_overrides={
            "files": files_registry,
            "read": read_registry,
            "search": search_registry,
        },
    )
