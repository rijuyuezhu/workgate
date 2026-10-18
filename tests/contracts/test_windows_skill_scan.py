"""Portable branch coverage of the native Windows Skill scan policy.

Real NT handle traversal is exercised by Windows Skill smoke tests. These
unit tests verify filtering and bounded reporting with deterministic entries.
"""

from pathlib import Path

import pytest

from workgate.platform.windows import skill_scan


def _rows(root: Path):
    return [
        (root / "SKILL.md", "file", 5, 0.0),
        (root / ".cache", "dir", None, 0.0),
        (root / ".cache/ignored.txt", "file", 7, 0.0),
        (root / "link.txt", "link", None, 0.0),
        (root / "asset.txt", "file", 4, 0.0),
        (root / "unsafe.txt", "file", 8, 0.0),
        (root / "nested/file.md", "file", 9, 0.0),
    ]


def test_require_skill_root_accepts_real_directory_and_missing_path(
    monkeypatch, tmp_path
):
    seen = []
    monkeypatch.setattr(
        skill_scan,
        "verify_directory",
        lambda path: seen.append(path),
    )
    assert skill_scan.require_skill_root(tmp_path)
    assert seen == [tmp_path]

    def absent(_path):
        raise FileNotFoundError("not here")

    monkeypatch.setattr(skill_scan, "verify_directory", absent)
    assert not skill_scan.require_skill_root(tmp_path)


def test_native_candidates_keep_only_allowed_directories_and_limit(
    monkeypatch, tmp_path
):
    root = tmp_path / "skills"

    def enumerate_entries(path, *, recursive, max_entries):
        assert path == root and recursive is False and max_entries == 4
        return [
            (root / "ok", "dir", None, 0.0),
            (root / "hidden", "dir", None, 0.0),
            (root / "link", "link", None, 0.0),
            (root / "SKILL.md", "file", 2, 0.0),
        ], True

    monkeypatch.setattr(skill_scan, "list_tree", enumerate_entries)
    names, count, truncated = skill_scan.candidate_directories(
        root, limit=4, excluded=frozenset({"hidden"})
    )
    assert names == ["ok"]
    assert count == 4
    assert truncated


def test_related_files_filters_reparse_special_and_bad_paths(
    monkeypatch, tmp_path
):
    root = tmp_path / "notes"

    def enumerate_entries(path, *, recursive, max_entries):
        assert path == root and recursive and max_entries == 50
        return _rows(root), True

    def validate(path):
        if path == "unsafe.txt":
            raise ValueError("rejected")
        return Path(path)

    monkeypatch.setattr(skill_scan, "list_tree", enumerate_entries)
    paths, warnings, scanned, used = skill_scan.related_files(
        root,
        entry_limit=50,
        file_limit=10,
        path_byte_limit=1024,
        ignored_dirs=frozenset({".cache"}),
        validate_path=validate,
    )
    assert paths == ["asset.txt", "nested/file.md"]
    assert scanned == 7
    assert used == len(b"asset.txtnested/file.md")
    assert any("unsafe.txt" in text for text in warnings)
    assert any("stopped after 50" in text for text in warnings)


@pytest.mark.parametrize(("file_limit", "path_limit"), [(1, 999), (9, 8)])
def test_related_files_respects_file_and_path_budgets(
    monkeypatch, tmp_path, file_limit, path_limit
):
    root = tmp_path / "notes"
    monkeypatch.setattr(
        skill_scan,
        "list_tree",
        lambda *_args, **_kwargs: (
            [
                (root / "asset.txt", "file", 4, 0.0),
                (root / "nested/file.md", "file", 3, 0.0),
            ],
            False,
        ),
    )
    paths, warnings, count, used = skill_scan.related_files(
        root,
        entry_limit=4,
        file_limit=file_limit,
        path_byte_limit=path_limit,
        ignored_dirs=frozenset(),
        validate_path=Path,
    )
    assert count == 2
    assert len(paths) == 1 if file_limit == 1 else len(paths) == 0
    assert any("truncated by budget" in text for text in warnings)
    assert used == sum(len(item.encode()) for item in paths)


def test_related_files_reports_native_enumeration_error(monkeypatch, tmp_path):
    def fail(*_args, **_kwargs):
        raise OSError("native list error")

    monkeypatch.setattr(skill_scan, "list_tree", fail)
    assert skill_scan.related_files(
        tmp_path,
        entry_limit=2,
        file_limit=2,
        path_byte_limit=100,
        ignored_dirs=frozenset(),
        validate_path=Path,
    ) == ([], ["Could not scan related files: native list error"], 0, 0)
