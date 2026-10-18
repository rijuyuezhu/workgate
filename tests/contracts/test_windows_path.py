"""Windows path syntax is host-independent; no Control-side normalization."""

from pathlib import Path, PureWindowsPath

import pytest

from workgate.app_paths import _windows_absolute_base, _windows_roots
from workgate.platform.path import remote_path_name


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        (r"C:\Users\alice\AppData\Roaming", True),
        (r"\\server\share\roaming", True),
        (r"C:relative\appdata", False),
        (r"\rooted-without-drive", False),
        (r"relative\appdata", False),
        ("", False),
    ],
)
def test_windows_environment_roots_require_absolute_native_paths(value, valid):
    result = _windows_absolute_base(value)
    assert (result is not None) is valid


def test_windows_app_roots_keep_roaming_and_local_separate():
    home = PureWindowsPath(r"C:\Users\alice")
    roaming, local = _windows_roots(
        {
            "APPDATA": r"D:\roaming",
            "LOCALAPPDATA": r"\\server\private\local",
        },
        home,
    )
    assert roaming == PureWindowsPath(r"D:\roaming")
    assert local == PureWindowsPath(r"\\server\private\local")


def test_windows_app_roots_reject_relative_environment_values():
    home = PureWindowsPath(r"C:\Users\alice")
    roaming, local = _windows_roots(
        {"APPDATA": "relative", "LOCALAPPDATA": r"C:bad\relative"}, home
    )
    assert roaming == home / "AppData" / "Roaming"
    assert local == home / "AppData" / "Local"


@pytest.mark.parametrize(
    ("path", "name"),
    [
        (r"C:\Users\alice\report.pdf", "report.pdf"),
        (r"\\server\share\project\notes.md", "notes.md"),
        (r"C:/Data\logs\run.txt", "run.txt"),
        ("/tmp/folder/file.txt", "file.txt"),
        ("relative/notes.md", "notes.md"),
        ("relative\\notes.md", "notes.md"),
        ("C:\\", ""),
    ],
)
def test_remote_path_basename_is_platform_independent(path, name):
    assert remote_path_name(path) == name


def test_windows_app_paths_use_windows_environment_roots():
    from workgate.app_paths import resolve_app_paths

    paths = resolve_app_paths(
        env={
            "APPDATA": r"C:\Users\alice\AppData\Roaming",
            "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
        },
        home=Path(r"C:\Users\alice"),
        platform="win32",
    )
    assert "Roaming" in str(paths.config_dir)
    assert "Local" in str(paths.state_dir)


def test_windows_lock_keys_fold_case_separators_and_dot_components():
    from pathlib import Path

    from workgate.platform.path import path_lock_key

    first = Path(r"C:\Foo\Work\file.txt")
    second = Path(r"c:/foo/work/..\WORK\FILE.TXT")
    assert path_lock_key(first, system="Windows") == path_lock_key(
        second, system="Windows"
    )
    assert path_lock_key(first, system="Linux") != path_lock_key(
        second, system="Linux"
    )


def test_control_download_filename_uses_executor_windows_basename():
    from workgate.control.downloads import _safe_download_filename

    assert (
        _safe_download_filename(None, r"C:\Users\alice\Documents\graph.png")
        == "graph.png"
    )
    assert (
        _safe_download_filename(None, r"\\server\share\2026\results.csv")
        == "results.csv"
    )


def test_posix_filename_can_contain_literal_backslashes():
    assert (
        remote_path_name(r"/home/alice/notes\draft.txt") == r"notes\draft.txt"
    )


@pytest.mark.parametrize(
    "name",
    [
        r"C:\absolute\secret",
        r"C:relative\secret",
        r"..\escape.txt",
        r"folder\..\escape.txt",
        "/absolute/path",
        r"\\server\share\file",
        "NUL",
        "CON.txt",
        "file:secret-stream",
        "filename.",
        "filename ",
    ],
)
def test_windows_archive_members_reject_native_path_escape_and_devices(name):
    from workgate.platform.path import archive_member_key

    with pytest.raises(ValueError, match="unsafe Windows archive member"):
        archive_member_key(name, system="Windows")


def test_windows_archive_member_keys_fold_case_and_separators():
    from workgate.platform.path import archive_member_key

    assert archive_member_key(
        "D/Note.TXT", system="Windows"
    ) == archive_member_key(r"d\note.txt", system="Windows")
    assert archive_member_key(
        "D/Note.TXT", system="Linux"
    ) != archive_member_key(r"d\note.txt", system="Linux")


def test_posix_archive_names_keep_original_pathlib_normalization():
    from workgate.platform.path import archive_member_key

    assert archive_member_key("dir/./note.txt", system="Linux") == (
        archive_member_key("dir/note.txt", system="Linux")
    )


def test_cross_platform_directory_listing_is_ordinary_pathlib(tmp_path):
    from workgate.executor.files import FilesConfig, _list_files_local

    folder = tmp_path / "directory"
    folder.mkdir()
    (folder / "plain.txt").write_bytes(b"foo")
    result = _list_files_local(
        FilesConfig(
            default_workdir=tmp_path,
            max_directory_entries=500,
            max_file_read_bytes=100000,
            max_file_write_bytes=100000,
        ),
        None,
        str(folder),
        recursive=True,
        max_entries=9,
    )
    assert result.count == 1
    assert result.entries[0].type == "file"
