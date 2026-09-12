import shutil

import pytest

from tests.helpers import build_tool_session_store
from workgate import persistence
from workgate.config import settings as settings_module
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.executor.config import resolve_executor_config
from workgate.executor.search import service as search_service_module
from workgate.executor.search.composition import (
    build_local_search_runner,
    build_search_service,
)
from workgate.executor.search.service import SearchRequest
from workgate.executor.tool_session import store as store_module
from workgate.executor.tool_session.bindings import SessionBinding
from workgate.schemas.result_models.search import (
    GrepMatch,
)


def _store_and_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    clear_settings_cache()
    settings = get_settings()
    store = build_tool_session_store(settings)
    store.clear()
    return store, settings


def _boom(*_args, **_kwargs):
    raise AssertionError(
        "migrated Search path reacquired an ambient dependency"
    )


def _session_id(index: int) -> str:
    return f"sess_{index:022d}"


@pytest.mark.asyncio
async def test_search_service_does_not_reacquire_ambient_dependencies(
    tmp_path, monkeypatch
):
    if not shutil.which("rg"):
        pytest.skip("missing rg")
    store, settings = _store_and_settings(tmp_path, monkeypatch)
    (tmp_path / "demo.txt").write_text("needle\n", encoding="utf-8")
    session = store.create_session(session_id=_session_id(1), workdir=tmp_path)
    service = build_search_service(resolve_executor_config(settings), store)

    monkeypatch.setattr(settings_module, "get_settings", _boom)
    monkeypatch.setattr(store_module, "get_tool_session_store", _boom)
    monkeypatch.setattr(persistence, "get_state_store", _boom)

    result = await service.search(
        session.session_id,
        "needle",
        regex=False,
        gitignore=False,
    )

    assert result.ok is True
    assert result.count == 1
    assert result.matches[0].session_id == session.session_id
    assert result.matches[0].snapshot_id is not None
    assert result.numbered_content


@pytest.mark.asyncio
async def test_search_service_resolves_fresh_workdir_per_operation(
    tmp_path, monkeypatch
):
    if not shutil.which("rg"):
        pytest.skip("missing rg")
    store, settings = _store_and_settings(tmp_path, monkeypatch)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "one.txt").write_text("needle first\n", encoding="utf-8")
    (second / "two.txt").write_text("needle second\n", encoding="utf-8")
    session = store.create_session(session_id=_session_id(2), workdir=first)
    service = build_search_service(resolve_executor_config(settings), store)

    first_result = await service.search(
        session.session_id,
        "needle",
        regex=False,
        gitignore=False,
    )
    store.change_session_workdir(session.session_id, second)
    second_result = await service.search(
        session.session_id,
        "needle",
        regex=False,
        gitignore=False,
    )

    assert [match.path for match in first_result.matches] == ["first/one.txt"]
    assert [match.path for match in second_result.matches] == ["second/two.txt"]


def test_search_path_access_and_scope_parsing_are_explicit(
    tmp_path, monkeypatch
):
    store, settings = _store_and_settings(tmp_path, monkeypatch)
    workdir = tmp_path / "work"
    workdir.mkdir()
    target = workdir / "demo.txt"
    target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    (workdir / "nested").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    runner = build_local_search_runner(resolve_executor_config(settings), store)

    assert (
        runner.paths.resolve_in_workdir(
            str(workdir), "demo.txt", must_exist=True
        )
        == target
    )
    assert runner.paths.display(target) == "work/demo.txt"
    with pytest.raises(ValueError, match="Path escapes session workdir"):
        runner.paths.resolve_in_workdir(
            str(workdir), "../outside.txt", must_exist=True
        )

    path_args, glob_args, line_scopes = runner._split_scopes(
        workdir,
        ["*.py", "demo.txt:1-2", "demo.txt:3-4"],
    )
    assert path_args == ["demo.txt"]
    assert glob_args == ["*.py"]
    assert line_scopes == {str(target): ((1, 4),)}

    root_args, root_globs, root_scopes = runner._split_scopes(workdir, ["."])
    assert root_args == ["."]
    assert root_globs == []
    assert root_scopes == {}

    outside_args, outside_globs, outside_scopes = runner._split_scopes(
        workdir, [str(outside)]
    )
    assert outside_args == [str(outside)]
    assert outside_globs == []
    assert outside_scopes == {}

    _path_args, _glob_args, unrestricted = runner._split_scopes(
        workdir,
        ["demo.txt:1-1", "demo.txt"],
    )
    assert unrestricted == {}
    _path_args, _glob_args, stays_unrestricted = runner._split_scopes(
        workdir,
        ["demo.txt", "demo.txt:1-1"],
    )
    assert stays_unrestricted == {}
    with pytest.raises(
        ValueError, match="line selectors are supported only for files"
    ):
        runner._split_scopes(workdir, ["nested:1-2"])


def test_search_grounding_failures_leave_matches_usable(tmp_path, monkeypatch):
    store, settings = _store_and_settings(tmp_path, monkeypatch)
    runner = build_local_search_runner(resolve_executor_config(settings), store)
    missing_line = GrepMatch(
        path="demo.txt", line=None, column=1, text="needle"
    )
    assert runner._ground_match(missing_line, tmp_path, None) == missing_line

    def fail_read(*_args, **_kwargs):
        raise ValueError("file changed")

    monkeypatch.setattr(
        search_service_module.SearchGrounding, "read", fail_read
    )
    match = GrepMatch(path="demo.txt", line=1, column=1, text="needle")
    assert runner._ground_match(match, tmp_path, None) == match
    displayed, numbered = runner._display_output(
        [search_service_module._RawSearchMatch(match, None)],
        tmp_path,
        None,
        1,
    )
    assert displayed == []
    assert numbered == ""


def test_search_grounding_projects_snapshots_and_display_windows(
    tmp_path, monkeypatch
):
    store, settings = _store_and_settings(tmp_path, monkeypatch)
    workdir = tmp_path / "work"
    workdir.mkdir()
    target = workdir / "demo.txt"
    target.write_text("alpha\nneedle here\ngamma\n", encoding="utf-8")
    session = store.create_session(session_id=_session_id(3), workdir=workdir)
    binding = SessionBinding(
        session_id=session.session_id,
        workdir=str(workdir),
    )
    runner = build_local_search_runner(resolve_executor_config(settings), store)

    plain = runner.grounding.read(str(target), 2, 2, binding=None)
    assert plain.path == "work/demo.txt"
    assert plain.snapshot_id is None
    assert plain.session_id is None

    grounded = runner.grounding.read(str(target), 2, 2, binding=binding)
    assert grounded.path == "work/demo.txt"
    assert grounded.session_id == session.session_id
    assert grounded.snapshot_id is not None

    match = GrepMatch(path="demo.txt", line=2, column=1, text="needle here")
    projected = runner._ground_match(match, workdir, binding)
    assert projected.path == "work/demo.txt"
    assert projected.numbered_line is not None
    assert projected.numbered_line.endswith("]\n2:needle here")
    assert projected.session_id == session.session_id
    assert projected.snapshot_id is not None
    assert projected.seen_range is not None
    assert projected.seen_range.model_dump() == {"start": 2, "end": 2}

    displayed, numbered = runner._display_output(
        [search_service_module._RawSearchMatch(match, None)],
        workdir,
        binding,
        1,
    )
    assert [line.line for line in displayed] == [1, 2, 3]
    assert [line.kind for line in displayed] == [
        "context",
        "match",
        "context",
    ]
    assert all(line.snapshot_id is not None for line in displayed)
    assert numbered.endswith("1:alpha\n2:needle here\n3:gamma")


@pytest.mark.asyncio
async def test_local_search_runner_parses_fake_rg_process_without_binary(
    tmp_path, monkeypatch
):
    store, settings = _store_and_settings(tmp_path, monkeypatch)
    target = tmp_path / "demo.txt"
    target.write_text("alpha\nneedle here\ngamma\n", encoding="utf-8")
    session = store.create_session(session_id=_session_id(4), workdir=tmp_path)
    binding = SessionBinding(
        session_id=session.session_id,
        workdir=str(tmp_path),
    )
    runner = build_local_search_runner(resolve_executor_config(settings), store)

    class FakeStdout:
        def __init__(self):
            self._lines = [
                b'{"type":"begin","data":{"path":{"text":"demo.txt"}}}\n',
                (
                    b'{"type":"match","data":{"path":{"text":"demo.txt"},'
                    b'"lines":{"text":"needle here\\n"},"line_number":2,'
                    b'"submatches":[{"start":0,"end":6}]}}\n'
                ),
                b"",
            ]

        async def readline(self) -> bytes:
            await search_service_module.asyncio.sleep(0)
            return self._lines.pop(0)

    class FakeStderr:
        async def read(self, _limit: int) -> bytes:
            return b"fake warning"

    class FakeProc:
        def __init__(self):
            self.stdout = FakeStdout()
            self.stderr = FakeStderr()
            self.returncode = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            raise AssertionError("normal fake search should not terminate")

        def kill(self) -> None:
            raise AssertionError("normal fake search should not kill")

    async def fake_spawn(*_args, **_kwargs):
        return FakeProc()

    monkeypatch.setattr(
        search_service_module.asyncio, "create_subprocess_exec", fake_spawn
    )
    result = await runner.search(
        SearchRequest(
            pattern="needle",
            paths="demo.txt:2-2",
            regex=False,
            max_results=10,
            gitignore=False,
        ),
        workdir=str(tmp_path),
        binding=binding,
    )

    assert result.ok is True
    assert result.count == 1
    assert result.matches[0].line == 2
    assert result.matches[0].snapshot_id is not None
    assert [line.line for line in result.displayed_lines] == [2]
    assert [line.kind for line in result.displayed_lines] == ["match"]
    assert result.stderr == "fake warning"
    assert result.truncated is False


@pytest.mark.asyncio
async def test_local_search_runner_reports_process_start_failure(
    tmp_path, monkeypatch
):
    store, settings = _store_and_settings(tmp_path, monkeypatch)
    runner = build_local_search_runner(resolve_executor_config(settings), store)

    async def fail_spawn(*_args, **_kwargs):
        raise OSError("not installed")

    monkeypatch.setattr(
        search_service_module.asyncio, "create_subprocess_exec", fail_spawn
    )
    result = await runner.search(
        SearchRequest(pattern="needle", regex=False, skip=-5),
        workdir=str(tmp_path),
    )

    assert result.ok is False
    assert result.count == 0
    assert result.skipped == 0
    assert "not installed" in result.stderr
