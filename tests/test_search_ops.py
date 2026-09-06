import asyncio
import shutil

import pytest

from workgate.config.settings import clear_settings_cache, get_settings
from workgate.ops import search as search_ops_module
from workgate.ops.files import files_config_from_settings
from workgate.ops.files_service import FilesService
from workgate.ops.search import (
    glob_search_execute,
    grep_search_execute,
    search_execute,
    tree_view_execute,
)
from workgate.tool_session.store import get_tool_session_store


def _create_session() -> str:
    store = get_tool_session_store()
    store.clear()
    return store.create_session(workdir=".").session_id


def _files_service() -> FilesService:
    return FilesService(
        files_config_from_settings(get_settings()),
        get_tool_session_store(),
        remote=None,
    )


@pytest.mark.asyncio
async def test_tree_reports_existing_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "project" / "src").mkdir(parents=True)
    (tmp_path / "project" / "README.md").write_text("hello", encoding="utf-8")

    session_id = _create_session()

    result = await tree_view_execute(session_id, "project")

    assert result.exists is True
    assert result.is_directory is True
    assert "src/" in result.entries
    assert "README.md" in result.entries


@pytest.mark.asyncio
async def test_tree_clamps_entries_without_sorting_entire_tree(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_MAX_TREE_ENTRIES", "3")
    clear_settings_cache()
    for idx in range(10):
        (tmp_path / f"file-{idx}.txt").write_text("x", encoding="utf-8")

    session_id = _create_session()

    result = await tree_view_execute(session_id, ".", max_entries=100)

    assert result.count == 3
    assert len(result.entries) == 3
    assert result.truncated is True


@pytest.mark.asyncio
async def test_tree_returns_context_for_missing_directory(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "actual").mkdir()

    session_id = _create_session()

    result = await tree_view_execute(session_id, "missing/project")

    assert result.exists is False
    assert result.is_directory is False
    assert result.nearest_existing_parent == str(tmp_path)
    assert result.nearest_parent_entries is not None
    assert "actual/" in result.nearest_parent_entries
    assert result.message is not None
    assert "Path does not exist" in result.message


@pytest.mark.asyncio
async def test_grep_accepts_query_starting_with_dash(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    if not shutil.which(get_settings().rg_bin):
        pytest.skip("missing rg")
    term = chr(45) + "needle"
    (tmp_path / "dash.txt").write_text(term + "\n", encoding="utf-8")

    result = await grep_search_execute(term, cwd=".", regex=False)

    assert result.ok is True
    assert result.count == 1
    assert result.matches[0].path is not None
    assert result.matches[0].path.endswith("dash.txt")


@pytest.mark.asyncio
async def test_grep_returns_leading_matches_when_output_is_large(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_MAX_GREP_RESULTS", "3")
    clear_settings_cache()
    if not shutil.which(get_settings().rg_bin):
        pytest.skip("missing rg")
    lines = "".join(f"needle {idx:04d} {'x' * 120}\n" for idx in range(5000))
    (tmp_path / "many.txt").write_text(lines, encoding="utf-8")

    result = await grep_search_execute(
        "needle", cwd=".", regex=False, max_results=3
    )

    assert result.ok is True
    assert result.truncated is True
    assert [match.line for match in result.matches] == [1, 2, 3]


@pytest.mark.asyncio
async def test_grep_returns_structured_error_when_rg_is_missing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_RG_BIN", "missing-rg-for-test")
    clear_settings_cache()
    (tmp_path / "app.py").write_text("needle\n", encoding="utf-8")

    result = await grep_search_execute("needle", cwd=".", regex=False)

    assert result.ok is False
    assert result.count == 0
    assert "missing-rg-for-test" in result.stderr


@pytest.mark.asyncio
async def test_grep_search_suppresses_cancelled_stderr_reader(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()

    class FakeStdout:
        async def readline(self) -> bytes:
            return b""

    class FakeStderr:
        async def read(self, limit: int) -> bytes:
            await asyncio.Event().wait()
            return b""

    class FakeProc:
        stdout = FakeStdout()
        stderr = FakeStderr()
        returncode = 0

        async def wait(self) -> int:
            return 0

        def terminate(self) -> None:
            raise AssertionError("terminate should not be needed")

        def kill(self) -> None:
            raise AssertionError("kill should not be needed")

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )

    result = await grep_search_execute("needle", cwd=".", regex=False)

    assert result.ok is True
    assert result.count == 0
    assert result.stderr == ""


@pytest.mark.asyncio
async def test_glob_finds_matching_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('x')", encoding="utf-8")
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")

    session_id = _create_session()

    result = await glob_search_execute(session_id, "*.py", cwd=".")

    assert result.paths == ["src/app.py"]


@pytest.mark.asyncio
async def test_remote_glob_tree_and_legacy_grep_facades_do_not_need_local_rg(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    store = get_tool_session_store()
    store.clear()
    session = store.create_session(
        target="remote",
        workdir="/remote/work",
        machine="worker-a",
        worker_session_id="WORKER01",
    )
    calls = []

    async def fake_remote_call(_session, tool, args):
        calls.append((tool, args))
        if tool == "glob_search":
            return {"paths": ["src/app.py"]}
        if tool == "tree_view":
            return {
                "root": "/remote/work",
                "exists": True,
                "is_directory": True,
                "entries": ["src/"],
                "count": 1,
                "truncated": False,
            }
        raise AssertionError(tool)

    monkeypatch.setattr(
        search_ops_module, "call_remote_session_tool", fake_remote_call
    )
    glob = await glob_search_execute(session.session_id, "*.py", cwd="src")
    tree = await tree_view_execute(session.session_id, cwd=".", depth=2)

    assert glob.paths == ["src/app.py"]
    assert tree.entries == ["src/"]
    assert calls == [
        (
            "glob_search",
            {"pattern": "*.py", "cwd": "src", "max_results": 500},
        ),
        ("tree_view", {"cwd": ".", "depth": 2, "max_entries": 500}),
    ]
    with pytest.raises(
        ValueError, match="grep_search_execute only supports local sessions"
    ):
        await grep_search_execute(
            "needle", session_id=session.session_id, regex=False
        )


@pytest.mark.asyncio
async def test_search_legacy_facade_routes_sessionless_and_explicit_cwd_without_rg(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    settings = get_settings()
    store = get_tool_session_store()
    store.clear()
    local_session = store.create_session(workdir=tmp_path)
    remote_session = store.create_session(
        target="remote",
        workdir="/remote/work",
        machine="worker-a",
        worker_session_id="WORKER01",
    )
    calls = []

    class FakeRunner:
        async def search(self, request, *, workdir, binding=None):
            calls.append(("local", request.pattern, workdir, binding))
            from workgate.schemas.result_models.search import (
                GrepSearchOutput,
            )

            return GrepSearchOutput(
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

    class FakeRemoteClient:
        async def search(self, binding, request):
            calls.append(("remote", request.pattern, binding.workdir, binding))
            from workgate.schemas.result_models.search import (
                GrepSearchOutput,
            )

            return GrepSearchOutput(
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

    monkeypatch.setattr(
        search_ops_module,
        "_legacy_search_dependencies",
        lambda: (settings, store),
    )
    monkeypatch.setattr(
        search_ops_module,
        "build_local_search_runner",
        lambda _settings, _store: FakeRunner(),
    )
    monkeypatch.setattr(
        search_ops_module, "RemoteSearchClient", lambda: FakeRemoteClient()
    )

    await search_execute("sessionless", cwd="custom", regex=False)
    await search_execute(
        "local",
        cwd=str(tmp_path),
        session_id=local_session.session_id,
        regex=False,
    )
    await search_execute(
        "remote",
        cwd="custom",
        session_id=remote_session.session_id,
        regex=False,
    )

    assert calls[0][:3] == ("local", "sessionless", "custom")
    assert calls[0][3] is None
    assert calls[1][:3] == ("local", "local", str(tmp_path))
    assert calls[1][3].session_id == local_session.session_id
    assert calls[2][:3] == ("remote", "remote", "/remote/work")
    assert calls[2][3].session_id == remote_session.session_id


@pytest.mark.asyncio
async def test_tree_and_glob_resolve_relative_to_session_workdir(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "outer.txt").write_text("outer", encoding="utf-8")
    (tmp_path / "project" / "src").mkdir(parents=True)
    (tmp_path / "project" / "src" / "app.py").write_text(
        "print('x')", encoding="utf-8"
    )

    store = get_tool_session_store()
    store.clear()
    session_id = store.create_session(workdir="project").session_id

    tree = await tree_view_execute(session_id, ".", depth=2)
    glob = await glob_search_execute(session_id, "*.py", cwd=".")

    assert tree.root == str(tmp_path / "project")
    assert "src/" in tree.entries
    assert "outer.txt" not in tree.entries
    assert glob.paths == ["project/src/app.py"]


@pytest.mark.asyncio
async def test_search_display_lines_resolve_from_session_workdir(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    if not shutil.which(get_settings().rg_bin):
        pytest.skip("missing rg")
    (tmp_path / "outer.txt").write_text("needle outside", encoding="utf-8")
    (tmp_path / "project" / "src").mkdir(parents=True)
    (tmp_path / "project" / "src" / "app.py").write_text(
        "alpha\nneedle here\ngamma\n", encoding="utf-8"
    )

    store = get_tool_session_store()
    store.clear()
    session_id = store.create_session(workdir="project").session_id

    result = await search_execute(
        "needle", paths="src", regex=False, session_id=session_id
    )

    assert result.ok is True
    assert result.count == 1
    assert result.matches[0].path == "project/src/app.py"
    assert result.matches[0].numbered_line is not None
    assert result.matches[0].numbered_line.startswith("[project/src/app.py#")
    assert result.displayed_count == 3
    assert [line.line for line in result.displayed_lines] == [1, 2, 3]
    assert [line.kind for line in result.displayed_lines] == [
        "context",
        "match",
        "context",
    ]
    assert result.numbered_content.startswith("[project/src/app.py#")
    assert "needle outside" not in result.numbered_content


@pytest.mark.asyncio
async def test_grep_search_returns_grounded_numbered_matches(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    if not shutil.which(get_settings().rg_bin):
        pytest.skip("missing rg")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "alpha\nneedle here\ngamma\n", encoding="utf-8"
    )

    session_id = _create_session()

    result = await grep_search_execute(
        "needle", cwd=".", regex=False, session_id=session_id
    )

    assert result.ok is True
    assert result.count == 1
    match = result.matches[0]
    assert match.path == "src/app.py"
    assert match.numbered_line is not None
    assert match.numbered_line.startswith("[src/app.py#")
    assert match.numbered_line.endswith("]\n2:needle here")
    assert match.snapshot_id
    assert match.file_sha256
    assert match.seen_range is not None
    assert match.seen_range.model_dump() == {"start": 2, "end": 2}
    assert result.displayed_count == 3
    assert result.context_radius == 1
    assert [line.line for line in result.displayed_lines] == [1, 2, 3]
    assert [line.kind for line in result.displayed_lines] == [
        "context",
        "match",
        "context",
    ]
    assert all(line.snapshot_id for line in result.displayed_lines)
    assert result.displayed_lines[0].seen_range is not None
    assert result.displayed_lines[0].seen_range.model_dump() == {
        "start": 1,
        "end": 3,
    }
    assert result.numbered_content.startswith("[src/app.py#")
    assert "1:alpha" in result.numbered_content
    assert "2:needle here" in result.numbered_content
    assert result.numbered_content.endswith("3:gamma")


@pytest.mark.asyncio
async def test_search_respects_gitignore_by_default_and_can_include_ignored(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    if not shutil.which(get_settings().rg_bin):
        pytest.skip("missing rg")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("needle\n", encoding="utf-8")

    session_id = _create_session()

    default_result = await search_execute(
        "needle", regex=False, session_id=session_id
    )
    explicit_result = await search_execute(
        "needle", regex=False, session_id=session_id, gitignore=True
    )
    no_ignore_result = await search_execute(
        "needle", regex=False, session_id=session_id, gitignore=False
    )

    assert [match.path for match in default_result.matches] == ["visible.txt"]
    assert [match.path for match in explicit_result.matches] == ["visible.txt"]
    assert {match.path for match in no_ignore_result.matches} == {
        "visible.txt",
        "ignored.txt",
    }


@pytest.mark.asyncio
async def test_search_merges_context_windows_for_multiple_matches(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    if not shutil.which(get_settings().rg_bin):
        pytest.skip("missing rg")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "one\nneedle alpha\nmiddle\nneedle beta\ntail\n",
        encoding="utf-8",
    )

    session_id = _create_session()

    result = await search_execute(
        "needle", paths="src", regex=False, session_id=session_id
    )

    assert result.count == 2
    assert [match.line for match in result.matches] == [2, 4]
    assert [line.line for line in result.displayed_lines] == [1, 2, 3, 4, 5]
    assert [line.kind for line in result.displayed_lines] == [
        "context",
        "match",
        "context",
        "match",
        "context",
    ]
    assert result.numbered_content.count("[src/app.py#") == 1


@pytest.mark.asyncio
async def test_hashline_edit_accepts_displayed_search_context_row(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    if not shutil.which(get_settings().rg_bin):
        pytest.skip("missing rg")
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "app.py"
    target.write_text("alpha\nneedle here\ngamma\n", encoding="utf-8")

    session_id = _create_session()
    result = await search_execute(
        "needle", paths="src", regex=False, session_id=session_id
    )

    header = next(
        line
        for line in result.numbered_content.splitlines()
        if line.startswith("[src/app.py#")
    )
    hashline_input = f"{header}\n1:alpha\n+ALPHA"
    await _files_service().hashline_edit(session_id, hashline_input)

    assert target.read_text(encoding="utf-8") == "ALPHA\nneedle here\ngamma\n"


@pytest.mark.asyncio
async def test_high_level_search_scopes_to_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    if not shutil.which(get_settings().rg_bin):
        pytest.skip("missing rg")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "tests" / "test_app.py").write_text(
        "needle\n", encoding="utf-8"
    )

    session_id = _create_session()

    result = await search_execute(
        "needle", paths="src", regex=False, session_id=session_id
    )

    assert result.ok is True
    assert result.count == 1
    assert result.matches[0].numbered_line is not None
    assert result.matches[0].numbered_line.startswith("[src/app.py#")
    assert result.matches[0].numbered_line.endswith("]\n1:needle")
    assert result.matches[0].path == "src/app.py"


@pytest.mark.asyncio
async def test_high_level_search_accepts_line_scoped_path_selector(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    if not shutil.which(get_settings().rg_bin):
        pytest.skip("missing rg")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "needle first\nneedle second\nneedle third\n", encoding="utf-8"
    )

    session_id = _create_session()

    result = await search_execute(
        "needle",
        paths="src/app.py:2-2",
        regex=False,
        session_id=session_id,
    )

    assert result.ok is True
    assert result.count == 1
    assert result.matches[0].path == "src/app.py"
    assert result.matches[0].line == 2
    assert result.matches[0].numbered_line is not None
    assert result.matches[0].numbered_line.startswith("[src/app.py#")
    assert result.matches[0].numbered_line.endswith("]\n2:needle second")
    assert [line.line for line in result.displayed_lines] == [2]
    assert [line.kind for line in result.displayed_lines] == ["match"]
    assert "1:needle first" not in result.numbered_content
    assert "3:needle third" not in result.numbered_content


@pytest.mark.asyncio
async def test_high_level_search_skip_pages_grounded_results(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    if not shutil.which(get_settings().rg_bin):
        pytest.skip("missing rg")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "needle first\nneedle second\nneedle third\n", encoding="utf-8"
    )

    session_id = _create_session()

    first = await search_execute(
        "needle",
        paths="src",
        regex=False,
        session_id=session_id,
        max_results=1,
    )
    second = await search_execute(
        "needle",
        paths="src",
        regex=False,
        session_id=session_id,
        max_results=1,
        skip=1,
    )

    assert first.count == 1
    assert first.skipped == 0
    assert first.matches[0].line == 1
    assert [line.line for line in first.displayed_lines] == [1, 2]
    assert [line.kind for line in first.displayed_lines] == ["match", "context"]
    assert first.truncated is True
    assert second.count == 1
    assert second.skipped == 1
    assert second.matches[0].line == 2
    assert second.matches[0].numbered_line is not None
    assert second.matches[0].numbered_line.startswith("[src/app.py#")
    assert second.matches[0].numbered_line.endswith("]\n2:needle second")
    assert [line.line for line in second.displayed_lines] == [1, 2, 3]
    assert [line.kind for line in second.displayed_lines] == [
        "context",
        "match",
        "context",
    ]


@pytest.mark.asyncio
async def test_high_level_search_paths_accept_line_scoped_file_selectors(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    if not shutil.which(get_settings().rg_bin):
        pytest.skip("missing rg")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "needle first\nignore\nneedle middle\nignore\nneedle last\n",
        encoding="utf-8",
    )

    session_id = _create_session()

    result = await search_execute(
        "needle",
        paths="src/app.py:3-3,5-5",
        regex=False,
        session_id=session_id,
    )

    assert result.ok is True
    assert result.count == 2
    assert [match.line for match in result.matches] == [3, 5]
    assert all(match.path == "src/app.py" for match in result.matches)
    assert result.matches[0].numbered_line is not None
    assert result.matches[0].numbered_line.startswith("[src/app.py#")
    assert result.matches[0].numbered_line.endswith("]\n3:needle middle")
    assert [line.line for line in result.displayed_lines] == [3, 5]
    assert [line.kind for line in result.displayed_lines] == ["match", "match"]
    assert "1:needle first" not in result.numbered_content
    assert "2:ignore" not in result.numbered_content
    assert "4:ignore" not in result.numbered_content


@pytest.mark.asyncio
async def test_high_level_search_rejects_invalid_line_selector(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    if not shutil.which(get_settings().rg_bin):
        pytest.skip("missing rg")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle\n", encoding="utf-8")

    session_id = _create_session()

    with pytest.raises(ValueError, match=r"invalid search line selector: 10\+"):
        await search_execute(
            "needle",
            paths="src/app.py:10+",
            regex=False,
            session_id=session_id,
        )


@pytest.mark.asyncio
async def test_search_numbered_content_can_feed_hashline_edit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    if not shutil.which(get_settings().rg_bin):
        pytest.skip("missing rg")
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "app.py"
    target.write_text("alpha\nneedle here\ngamma\n", encoding="utf-8")

    session_id = _create_session()
    result = await search_execute(
        "needle", paths="src", regex=False, session_id=session_id
    )

    header = result.numbered_content.splitlines()[0]
    assert header.startswith("[src/app.py#")
    hashline_input = f"{header}\n1:alpha\n+ALPHA"
    await _files_service().hashline_edit(session_id, hashline_input)

    assert target.read_text(encoding="utf-8") == "ALPHA\nneedle here\ngamma\n"


@pytest.mark.asyncio
async def test_high_level_search_merges_repeated_line_scoped_file_selectors(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    if not shutil.which(get_settings().rg_bin):
        pytest.skip("missing rg")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "needle first\nignore\nneedle middle\nignore\nneedle last\n",
        encoding="utf-8",
    )

    session_id = _create_session()

    result = await search_execute(
        "needle",
        paths=["src/app.py:1-1", "src/app.py:5-5"],
        regex=False,
        session_id=session_id,
    )

    assert result.ok is True
    assert [match.line for match in result.matches] == [1, 5]
    assert [line.line for line in result.displayed_lines] == [1, 5]
    assert "1:needle first" in result.numbered_content
    assert "5:needle last" in result.numbered_content
    assert "3:needle middle" not in result.numbered_content


@pytest.mark.asyncio
async def test_mcp_search_facade_returns_grounded_results(
    tmp_path, monkeypatch
):
    from tests.helpers import mcp_structured
    from workgate.control.mcp.app import build_mcp

    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    if not shutil.which(get_settings().rg_bin):
        pytest.skip("missing rg")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle\n", encoding="utf-8")

    session = mcp_structured(
        await build_mcp().call_tool("session_start", {"workdir": "."})
    )
    payload = mcp_structured(
        await build_mcp().call_tool(
            "search",
            {
                "session_id": session["session_id"],
                "pattern": "needle",
                "paths": "src",
                "regex": False,
            },
        )
    )

    assert payload["matches"][0]["path"] == "src/app.py"
    assert payload["matches"][0]["snapshot_id"]
    assert payload["displayed_lines"][0]["line"] == 1
    assert payload["displayed_lines"][0]["kind"] == "match"
    assert payload["displayed_lines"][0]["snapshot_id"]
    assert payload["displayed_count"] == 1
    assert payload["numbered_content"].startswith("[src/app.py#")
    assert payload["numbered_content"].endswith("]\n1:needle")
