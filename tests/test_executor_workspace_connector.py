from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from workgate.executor.files_service import FilesService
from workgate.executor.search.service import SearchService
from workgate.executor.workspace_connector import WorkspaceConnectorService


class _FakeSearchService:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def search(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return SimpleNamespace(
            matches=[
                SimpleNamespace(path="src/a.py", line=7),
                SimpleNamespace(path="src/a.py", line=9),
                SimpleNamespace(path="", line=1),
                SimpleNamespace(path="README.md", line=None),
            ]
        )


class _FakeFilesService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def read_file(self, session_id: str, result_id: str) -> Any:
        self.calls.append((session_id, result_id))
        return SimpleNamespace(
            path=result_id,
            content="workspace content",
            bytes=17,
        )


@pytest.mark.asyncio
async def test_workspace_connector_search_projects_unique_file_results() -> (
    None
):
    search = _FakeSearchService()
    files = _FakeFilesService()
    service = WorkspaceConnectorService(
        cast(SearchService, search), cast(FilesService, files)
    )

    result = await service.search("sess-1", "Needle")

    assert [row.model_dump() for row in result.results] == [
        {
            "id": "src/a.py",
            "title": "src/a.py:7",
            "url": "file:///workspace/src/a.py",
        },
        {
            "id": "README.md",
            "title": "README.md",
            "url": "file:///workspace/README.md",
        },
    ]
    assert search.calls == [
        (
            ("sess-1", "Needle"),
            {
                "regex": False,
                "case_sensitive": False,
                "max_results": 20,
            },
        )
    ]


@pytest.mark.asyncio
async def test_workspace_connector_fetch_projects_executor_file() -> None:
    search = _FakeSearchService()
    files = _FakeFilesService()
    service = WorkspaceConnectorService(
        cast(SearchService, search), cast(FilesService, files)
    )

    result = await service.fetch("sess-1", "notes.txt")

    assert result.model_dump() == {
        "id": "notes.txt",
        "title": "notes.txt",
        "text": "workspace content",
        "url": "file:///workspace/notes.txt",
        "metadata": {"source": "workspace", "bytes": 17},
    }
    assert files.calls == [("sess-1", "notes.txt")]
