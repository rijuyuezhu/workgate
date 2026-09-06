import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import workgate.ops.files as files_ops
import workgate.tools.registry.files as files_registry
from tests.helpers import nested_mcp_text
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.mcp.app import build_mcp
from workgate.ops.files import (
    delete_file_or_dir_execute,
    list_files_execute,
    parse_hashline_edit_input,
    read_file_execute,
    write_file_execute,
)
from workgate.ops.shell import check_command_policy
from workgate.ops.utils.path import resolve_path
from workgate.tool_session.bindings import LocalSessionBinding
from workgate.tool_session.resolver import SessionResolver
from workgate.tool_session.store import get_tool_session_store


def _create_session() -> str:
    store = get_tool_session_store()
    store.clear()
    return store.create_session(workdir=".").session_id


def _local_binding(session_id: str | None) -> LocalSessionBinding | None:
    if session_id is None:
        return None
    binding = SessionResolver(get_tool_session_store()).resolve_active_binding(
        session_id
    )
    assert isinstance(binding, LocalSessionBinding)
    return binding


def _edit_lines(
    path: str,
    start_line: int,
    end_line: int,
    replacement: str,
    snapshot_id: str | None = None,
    session_id: str | None = None,
):
    store = get_tool_session_store()
    return files_ops._edit_lines_local(
        files_ops.files_config_from_settings(get_settings()),
        store,
        _local_binding(session_id),
        path,
        start_line,
        end_line,
        replacement,
        snapshot_id,
    )


def _hashline_edit(input_text: str, session_id: str | None = None):
    store = get_tool_session_store()
    return files_ops._hashline_edit_local(
        files_ops.files_config_from_settings(get_settings()),
        store,
        _local_binding(session_id),
        input_text,
    )


def test_write_and_read_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    write_file_execute("a.txt", "hello world")
    assert read_file_execute("a.txt").content == "hello world"


@pytest.mark.asyncio
async def test_registered_file_handlers_round_trip_grounded_edits(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    session_id = _create_session()

    written = await files_registry.write_file.func(
        session_id, "adapter.txt", "one\ntwo\n"
    )
    assert written.created is True

    first = read_file_execute(
        "adapter.txt", start_line=1, end_line=2, session_id=session_id
    )
    assert first.snapshot_id is not None
    await files_registry.edit_lines.func(
        "adapter.txt",
        2,
        2,
        "TWO",
        session_id,
        first.snapshot_id,
    )

    second = read_file_execute(
        "adapter.txt", start_line=1, end_line=2, session_id=session_id
    )
    assert second.snapshot_id is not None
    await files_registry.hashline_edit.func(
        session_id,
        f"[adapter.txt#{second.snapshot_id}]\n1:one\n+ONE",
    )
    assert (tmp_path / "adapter.txt").read_text(encoding="utf-8") == (
        "ONE\nTWO\n"
    )

    deleted = await files_registry.delete_file_or_dir.func(
        session_id, "adapter.txt"
    )
    assert deleted.deleted == "file"
    assert not (tmp_path / "adapter.txt").exists()


def test_list_files_reports_limit_and_truncation(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    listed_dir = tmp_path / "listed"
    listed_dir.mkdir()
    (listed_dir / "a.txt").write_text("a", encoding="utf-8")
    (listed_dir / "b.txt").write_text("b", encoding="utf-8")

    limited = list_files_execute("listed", max_entries=1)
    complete = list_files_execute("listed", max_entries=10)

    assert limited.limit_count == 1
    assert limited.count == 1
    assert limited.is_truncated is True
    assert len(limited.entries) == 1
    assert "total_count" not in limited.model_dump()

    assert complete.count == 2
    assert complete.is_truncated is False


@pytest.mark.skipif(
    os.name == "nt", reason="symlink creation may require elevated privileges"
)
def test_list_and_delete_preserve_final_symlink(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    target = tmp_path / "target"
    target.mkdir()
    important = target / "important.txt"
    important.write_text("keep", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    entries = {entry.path: entry for entry in list_files_execute(".").entries}

    assert entries["link"].type == "link"
    assert entries["link"].target == str(target)
    assert entries["target"].type == "dir"

    deleted = delete_file_or_dir_execute("link")

    assert deleted.model_dump() == {"path": "link", "deleted": "link"}
    assert not os.path.lexists(link)
    assert important.read_text(encoding="utf-8") == "keep"


def test_read_text_rejects_invalid_utf8(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "invalid.bin").write_bytes(b"\xff\xfe\xfd")

    with pytest.raises(UnicodeDecodeError):
        read_file_execute("invalid.bin")


def test_read_text_allows_valid_utf8_control_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "nul.txt").write_bytes(b"abc\x00def")

    result = read_file_execute("nul.txt")

    assert result.content == "abc\x00def"


def test_read_text_returns_line_numbers_and_snapshot_metadata(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "lines.txt").write_text(
        "alpha\nbeta\ngamma\n", encoding="utf-8"
    )

    session_id = _create_session()

    result = read_file_execute(
        "lines.txt", start_line=2, end_line=3, session_id=session_id
    )

    assert result.content == "beta\ngamma"
    assert result.start_line == 2
    assert result.end_line == 3
    assert result.line_count == 2
    assert result.lines[0].line == 2
    assert result.lines[0].text == "beta"
    assert result.numbered_content.startswith("[lines.txt#")
    assert result.numbered_content.endswith("]\n2:beta\n3:gamma")
    assert result.session_id == session_id
    assert result.snapshot_id
    assert result.file_sha256
    assert [item.model_dump() for item in result.seen_ranges] == [
        {"start": 2, "end": 3}
    ]


def test_read_text_reports_original_size_and_truncation(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_MAX_FILE_READ_BYTES", "5")
    clear_settings_cache()
    (tmp_path / "long.txt").write_text("hello world", encoding="utf-8")

    result = read_file_execute("long.txt")

    assert result.bytes == 11
    assert result.bytes_read == 5
    assert result.truncated_bytes == 6
    assert result.truncated is True
    assert result.content == "hello"
    assert result.numbered_content == "1:hello"


def test_write_text_does_not_read_existing_file_before_overwrite(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "existing.txt").write_text("old", encoding="utf-8")

    def fail_read_text(self, *args, **kwargs):
        raise AssertionError(
            "write_file_execute should not read old file contents"
        )

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    result = write_file_execute("existing.txt", "new")

    assert result.created is False
    assert (tmp_path / "existing.txt").read_bytes().decode("utf-8") == "new"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not portable")
def test_atomic_write_preserves_existing_file_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    path = tmp_path / "mode.txt"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o640)

    write_file_execute("mode.txt", "new")

    assert path.read_text(encoding="utf-8") == "new"
    assert path.stat().st_mode & 0o777 == 0o640


def test_concurrent_overwrite_false_creates_file_once(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    barrier = threading.Barrier(2)

    def write(content: str) -> str:
        barrier.wait(timeout=5)
        try:
            write_file_execute("shared.txt", content, overwrite=False)
        except FileExistsError:
            return "exists"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(write, ["first", "second"]))

    assert sorted(outcomes) == ["created", "exists"]
    assert (tmp_path / "shared.txt").read_text(encoding="utf-8") in {
        "first",
        "second",
    }


def test_concurrent_snapshot_edits_reject_stale_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    path = tmp_path / "shared.txt"
    path.write_text("alpha\nbeta\n", encoding="utf-8")
    session_id = _create_session()
    snapshot = read_file_execute(
        "shared.txt", start_line=1, end_line=2, session_id=session_id
    )
    assert snapshot.snapshot_id is not None
    barrier = threading.Barrier(2)

    def edit(replacement: str) -> str:
        barrier.wait(timeout=5)
        try:
            _edit_lines(
                "shared.txt",
                2,
                2,
                replacement,
                snapshot_id=snapshot.snapshot_id,
                session_id=session_id,
            )
        except ValueError as exc:
            assert "file changed since snapshot" in str(exc)
            return "stale"
        return "edited"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(edit, ["BETA-ONE", "BETA-TWO"]))

    assert sorted(outcomes) == ["edited", "stale"]
    assert path.read_text(encoding="utf-8") in {
        "alpha\nBETA-ONE\n",
        "alpha\nBETA-TWO\n",
    }


@pytest.mark.asyncio
async def test_fetch_reports_non_utf8_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "blob.bin").write_bytes(b"abc\xffworld")

    response = await build_mcp().call_tool("fetch", {"id": "blob.bin"})
    payload = json.loads(nested_mcp_text(response))

    assert payload["text"].startswith(
        "Unable to fetch file: UnicodeDecodeError:"
    )
    assert payload["metadata"]["error"] == "UnicodeDecodeError"


def test_reject_path_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_ALLOW_FULL_CONTROL", "false")
    clear_settings_cache()
    with pytest.raises(ValueError):
        resolve_path("/etc/passwd")


def test_full_container_mode_disables_builtin_restrictions(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_ALLOW_FULL_CONTROL", "true")
    clear_settings_cache()

    settings = get_settings()
    assert settings.command_denylist == []

    assert settings.path_denylist == []
    outside_workspace = Path(tmp_path.anchor) / "outside-workspace"
    assert resolve_path(outside_workspace) == Path(
        os.path.abspath(outside_workspace)
    )
    check_command_policy("mount /dev/null /mnt || true")


def test_read_text_handles_truncated_utf8_sequence(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_MAX_FILE_READ_BYTES", "4")
    clear_settings_cache()
    (tmp_path / "utf8.txt").write_text("你好", encoding="utf-8")

    result = read_file_execute("utf8.txt")

    assert result.truncated is True
    assert result.bytes_read == 4
    assert result.content == "你"


def test_parse_read_target_supports_line_and_raw_selectors():
    from workgate.tool_session.selectors import parse_read_target

    assert parse_read_target("src/foo.py:50-80").path == "src/foo.py"
    ranged = parse_read_target("src/foo.py:50+20:raw")
    assert ranged.path == "src/foo.py"
    assert ranged.start_line == 50
    assert ranged.end_line == 69
    assert ranged.line_ranges == ((50, 69),)
    assert ranged.raw is True
    raw_first = parse_read_target("src/foo.py:raw:10-12")
    assert raw_first.start_line == 10
    assert raw_first.end_line == 12
    assert raw_first.line_ranges == ((10, 12),)
    assert raw_first.raw is True
    multi = parse_read_target("src/foo.py:5-6,10+2:raw")
    assert multi.path == "src/foo.py"
    assert multi.start_line == 5
    assert multi.end_line == 11
    assert multi.line_ranges == ((5, 6), (10, 11))
    assert multi.raw is True


@pytest.mark.parametrize(
    "target",
    [
        "src/foo.py:10-5",
        "src/foo.py:5-8,7-9",
        "src/foo.py:5,10",
        "src/foo.py:5,,10",
    ],
)
def test_parse_read_target_rejects_invalid_multi_range_selectors(target):
    from workgate.tool_session.selectors import parse_read_target

    with pytest.raises(ValueError):
        parse_read_target(target)


def test_read_file_execute_multi_ranges_records_grounding_and_edits(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "multi.py").write_text(
        "one\ntwo\nthree\nfour\nfive\n", encoding="utf-8"
    )
    session_id = _create_session()

    read_result = read_file_execute(
        "multi.py",
        session_id=session_id,
        line_ranges=((2, 3), (5, 5)),
    )

    assert [line.line for line in read_result.lines] == [2, 3, 5]
    assert read_result.start_line == 2
    assert read_result.end_line == 5
    assert read_result.line_count == 3
    assert [line.model_dump() for line in read_result.seen_ranges] == [
        {"start": 2, "end": 3},
        {"start": 5, "end": 5},
    ]
    assert read_result.numbered_content.startswith("[multi.py#")
    assert "2:two" in read_result.numbered_content
    assert "3:three" in read_result.numbered_content
    assert "5:five" in read_result.numbered_content
    assert "4:four" not in read_result.numbered_content
    assert read_result.snapshot_id is not None

    with pytest.raises(ValueError, match="not shown"):
        _edit_lines(
            "multi.py", 4, 4, "FOUR", read_result.snapshot_id, session_id
        )

    _edit_lines("multi.py", 5, 5, "FIVE", read_result.snapshot_id, session_id)
    assert (tmp_path / "multi.py").read_text(encoding="utf-8") == (
        "one\ntwo\nthree\nfour\nFIVE\n"
    )


def test_edit_lines_uses_snapshot_and_returns_diff_context(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "edit.py").write_text(
        "alpha\nbeta\ngamma\ndelta\n", encoding="utf-8"
    )
    session_id = _create_session()
    read_result = read_file_execute(
        "edit.py", start_line=2, end_line=3, session_id=session_id
    )

    result = _edit_lines(
        "edit.py",
        2,
        3,
        "BETA\nGAMMA",
        snapshot_id=read_result.snapshot_id,
        session_id=read_result.session_id,
    )

    assert (tmp_path / "edit.py").read_text(encoding="utf-8") == (
        "alpha\nBETA\nGAMMA\ndelta\n"
    )
    assert result.replacement_line_count == 2
    assert "-beta" in result.diff
    assert "+BETA" in result.diff
    assert result.context.numbered_content.startswith("[edit.py#")
    assert result.context.numbered_content.endswith(
        "]\n1:alpha\n2:BETA\n3:GAMMA\n4:delta"
    )
    assert result.context.snapshot_id != read_result.snapshot_id


def test_edit_lines_rejects_stale_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "edit.py").write_text("alpha\nbeta\n", encoding="utf-8")
    session_id = _create_session()
    read_result = read_file_execute(
        "edit.py", start_line=1, end_line=1, session_id=session_id
    )
    (tmp_path / "edit.py").write_text("changed\nbeta\n", encoding="utf-8")

    with pytest.raises(ValueError, match="file changed since snapshot"):
        _edit_lines(
            "edit.py",
            1,
            1,
            "ALPHA",
            snapshot_id=read_result.snapshot_id,
            session_id=read_result.session_id,
        )


def test_edit_lines_rejects_unseen_snapshot_range(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "edit.py").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    session_id = _create_session()
    read_result = read_file_execute(
        "edit.py", start_line=1, end_line=1, session_id=session_id
    )

    with pytest.raises(ValueError, match="edit range was not shown"):
        _edit_lines(
            "edit.py",
            2,
            2,
            "BETA",
            snapshot_id=read_result.snapshot_id,
            session_id=read_result.session_id,
        )


def test_hashline_edit_replaces_copied_line_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "edit.py").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    session_id = _create_session()
    read_result = read_file_execute(
        "edit.py", start_line=2, end_line=2, session_id=session_id
    )

    result = _hashline_edit(
        f"[edit.py#{read_result.snapshot_id}]\n2:beta\n+BETA",
        session_id=session_id,
    )

    assert (tmp_path / "edit.py").read_text(encoding="utf-8") == (
        "alpha\nBETA\ngamma\n"
    )
    assert result.start_line == 2
    assert result.end_line == 2
    assert result.context.numbered_content.startswith("[edit.py#")
    assert result.context.snapshot_id != read_result.snapshot_id


def test_hashline_edit_deletes_when_no_replacement_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "edit.py").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    session_id = _create_session()
    read_result = read_file_execute(
        "edit.py", start_line=2, end_line=2, session_id=session_id
    )

    _hashline_edit(
        f"[edit.py#{read_result.snapshot_id}]\n2:beta",
        session_id=session_id,
    )

    assert (tmp_path / "edit.py").read_text(encoding="utf-8") == (
        "alpha\ngamma\n"
    )


def test_hashline_edit_supports_swap_directive(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "edit.py").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    session_id = _create_session()
    read_result = read_file_execute(
        "edit.py", start_line=2, end_line=3, session_id=session_id
    )

    _hashline_edit(
        f"[edit.py#{read_result.snapshot_id}]\nSWAP 2-3:\n+BETA\n+GAMMA",
        session_id=session_id,
    )

    assert (tmp_path / "edit.py").read_text(encoding="utf-8") == (
        "alpha\nBETA\nGAMMA\n"
    )


def test_hashline_edit_supports_insert_directive(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "edit.py").write_text("alpha\nbeta\n", encoding="utf-8")
    session_id = _create_session()
    read_result = read_file_execute(
        "edit.py", start_line=2, end_line=2, session_id=session_id
    )

    _hashline_edit(
        f"[edit.py#{read_result.snapshot_id}]\nINSERT BEFORE 2:\n+inserted",
        session_id=session_id,
    )

    assert (tmp_path / "edit.py").read_text(encoding="utf-8") == (
        "alpha\ninserted\nbeta\n"
    )


def test_hashline_edit_accepts_workspace_relative_header_from_nested_session(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    project = tmp_path / "project"
    project.mkdir()
    (project / "edit.py").write_text("alpha\nbeta\n", encoding="utf-8")
    store = get_tool_session_store()
    store.clear()
    session_id = store.create_session(workdir="project").session_id
    read_result = read_file_execute(
        "edit.py", start_line=2, end_line=2, session_id=session_id
    )

    assert read_result.path == "project/edit.py"
    payload = (
        "[project/edit.py#"
        + str(read_result.snapshot_id)
        + "]"
        + chr(10)
        + "2:beta"
        + chr(10)
        + "+BETA"
    )
    _hashline_edit(payload, session_id=session_id)

    assert (project / "edit.py").read_text(encoding="utf-8") == (
        "alpha\nBETA\n"
    )


def test_hashline_edit_rejects_mismatched_old_text(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "edit.py").write_text("alpha\nbeta\n", encoding="utf-8")
    session_id = _create_session()
    read_result = read_file_execute(
        "edit.py", start_line=2, end_line=2, session_id=session_id
    )

    with pytest.raises(ValueError, match="old text does not match"):
        _hashline_edit(
            f"[edit.py#{read_result.snapshot_id}]\n2:not beta\n+BETA",
            session_id=session_id,
        )


def test_parse_hashline_edit_rejects_non_consecutive_rows():
    with pytest.raises(ValueError, match="consecutive"):
        parse_hashline_edit_input("[a.txt#snap]\n2:b\n4:d\n+x")


def test_hashline_edit_supports_multiple_hunks_same_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "edit.py").write_text(
        "alpha\nbeta\ngamma\ndelta\n", encoding="utf-8"
    )
    session_id = _create_session()
    read_result = read_file_execute(
        "edit.py", start_line=1, end_line=4, session_id=session_id
    )

    result = _hashline_edit(
        f"[edit.py#{read_result.snapshot_id}]\n"
        "2:beta\n"
        "+BETA\n"
        "\n"
        "4:delta\n"
        "+DELTA",
        session_id=session_id,
    )

    assert (tmp_path / "edit.py").read_text(encoding="utf-8") == (
        "alpha\nBETA\ngamma\nDELTA\n"
    )
    assert result.hunk_count == 2
    assert [(h.start_line, h.end_line) for h in result.hunks] == [
        (2, 2),
        (4, 4),
    ]
    assert all(h.context.snapshot_id for h in result.hunks)


def test_hashline_edit_supports_multiple_hunks_with_insert(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "edit.py").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    session_id = _create_session()
    read_result = read_file_execute(
        "edit.py", start_line=1, end_line=3, session_id=session_id
    )

    result = _hashline_edit(
        f"[edit.py#{read_result.snapshot_id}]\n"
        "INSERT AFTER 1:\n"
        "+inserted\n"
        "\n"
        "3:gamma\n"
        "+GAMMA",
        session_id=session_id,
    )

    assert (tmp_path / "edit.py").read_text(encoding="utf-8") == (
        "alpha\ninserted\nbeta\nGAMMA\n"
    )
    assert result.hunk_count == 2
    assert result.hunks[0].replacement_line_count == 2
    assert result.hunks[1].replacement_line_count == 1


def test_hashline_edit_supports_multiple_files(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "one.py").write_text("alpha\nbeta\n", encoding="utf-8")
    (tmp_path / "two.py").write_text("gamma\ndelta\n", encoding="utf-8")
    session_id = _create_session()
    one = read_file_execute(
        "one.py", start_line=2, end_line=2, session_id=session_id
    )
    two = read_file_execute(
        "two.py", start_line=1, end_line=1, session_id=session_id
    )

    result = _hashline_edit(
        f"[one.py#{one.snapshot_id}]\n"
        "2:beta\n"
        "+BETA\n"
        f"[two.py#{two.snapshot_id}]\n"
        "1:gamma\n"
        "+GAMMA",
        session_id=session_id,
    )

    assert (tmp_path / "one.py").read_text(encoding="utf-8") == "alpha\nBETA\n"
    assert (tmp_path / "two.py").read_text(encoding="utf-8") == "GAMMA\ndelta\n"
    assert result.hunk_count == 2
    assert [h.path for h in result.hunks] == ["one.py", "two.py"]


def test_hashline_edit_rejects_overlapping_hunks(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    clear_settings_cache()
    (tmp_path / "edit.py").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    session_id = _create_session()
    read_result = read_file_execute(
        "edit.py", start_line=1, end_line=3, session_id=session_id
    )

    with pytest.raises(ValueError, match="overlap"):
        _hashline_edit(
            f"[edit.py#{read_result.snapshot_id}]\n"
            "1:alpha\n"
            "2:beta\n"
            "+ALPHA\n"
            "+BETA\n"
            "\n"
            "2:beta\n"
            "3:gamma\n"
            "+BETA\n"
            "+GAMMA",
            session_id=session_id,
        )
