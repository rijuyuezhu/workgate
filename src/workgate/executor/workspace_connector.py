"""Executor-owned connector-compatible workspace search/fetch projection."""

from dataclasses import dataclass

from ..tools.schemas.result_models.workspace_connector import (
    FetchOutput,
    SearchOutput,
    SearchResult,
)
from .files_service import FilesService
from .search.service import SearchService


@dataclass(frozen=True)
class WorkspaceConnectorService:
    """Project connector responses from one explicit executor-backed session."""

    search_service: SearchService
    files_service: FilesService

    async def search(self, session_id: str, query: str) -> SearchOutput:
        result = await self.search_service.search(
            session_id,
            query,
            regex=False,
            case_sensitive=False,
            max_results=20,
        )
        seen: set[str] = set()
        rows: list[SearchResult] = []
        for match in result.matches:
            path = match.path
            if not path or path in seen:
                continue
            seen.add(path)
            suffix = f":{match.line}" if match.line else ""
            rows.append(
                SearchResult(
                    id=path,
                    title=f"{path}{suffix}",
                    url=f"file:///workspace/{path}",
                )
            )
        return SearchOutput(results=rows)

    async def fetch(self, session_id: str, result_id: str) -> FetchOutput:
        read_result = await self.files_service.read_file(session_id, result_id)
        return FetchOutput(
            id=read_result.path,
            title=read_result.path,
            text=read_result.content,
            url=f"file:///workspace/{read_result.path}",
            metadata={"source": "workspace", "bytes": read_result.bytes},
        )
