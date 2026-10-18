"""Windows path syntax is host-independent; no Control-side normalization."""

from pathlib import PureWindowsPath

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


def test_windows_private_directory_dispatches_to_native_acl_backend(
    monkeypatch, tmp_path
):
    import workgate.app_paths as app_paths_module
    from workgate.app_paths import ensure_private_directory
    from workgate.platform.host import HostOS
    from workgate.platform.windows import security

    monkeypatch.setattr(app_paths_module, "host_os", lambda: HostOS.WINDOWS)
    private = tmp_path / "credentials"
    calls = []

    def fake_create(path):
        calls.append(path)
        return path

    monkeypatch.setattr(security, "ensure_private_directory", fake_create)
    assert ensure_private_directory(private) == private
    assert calls == [private]


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


def test_windows_no_follow_resolution_keeps_ancestor_junction_syntax(
    tmp_path, monkeypatch
):
    from workgate.executor import path as executor_path
    from workgate.platform.host import HostOS

    target = tmp_path / "target"
    target.mkdir()
    (target / "item.txt").write_bytes(b"target")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(executor_path, "host_os", lambda: HostOS.WINDOWS)
    resolved = executor_path.resolve_path(
        "link/item.txt", base=tmp_path, follow_final_symlink=False
    )
    assert resolved == link / "item.txt"
    assert resolved.parent == link


def test_windows_scratch_prune_deletes_only_through_verified_private_handle(
    monkeypatch, tmp_path
):
    import workgate.executor.path as executor_path
    from workgate.platform.host import HostOS
    from workgate.platform.windows import security

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    abandoned = scratch / "old.tmp"
    abandoned.write_bytes(b"temporary")
    seen = []

    def native_delete(path):
        seen.append(path)
        path.unlink()

    monkeypatch.setattr(executor_path, "host_os", lambda: HostOS.WINDOWS)
    monkeypatch.setattr(security, "delete_private_file", native_delete)
    executor_path.prune_temp_dir(
        max_files=0,
        max_bytes=0,
        directory=scratch,
        minimum_age_s=0,
    )
    assert seen == [abandoned]
    assert not abandoned.exists()


def test_windows_directory_listing_dispatches_through_verified_native_handle(
    monkeypatch, tmp_path
):
    import workgate.executor.files as files_module
    from workgate.executor.files import FilesConfig
    from workgate.platform.host import HostOS
    from workgate.platform.windows import archive

    root = tmp_path / "directory"
    root.mkdir()
    (root / "plain.txt").write_bytes(b"foo")
    monkeypatch.setattr(files_module, "host_os", lambda: HostOS.WINDOWS)
    calls = []

    def secure_listing(base, *, recursive, max_entries):
        calls.append((base, recursive, max_entries))
        return ([(base / "plain.txt", "file", 3, 1_750_000_000.0)], False)

    monkeypatch.setattr(archive, "list_tree", secure_listing)
    config = FilesConfig(
        default_workdir=tmp_path,
        max_directory_entries=500,
        max_file_read_bytes=100000,
        max_file_write_bytes=100000,
    )
    result = files_module._list_files_local(
        config, None, str(root), recursive=True, max_entries=9
    )
    assert calls == [(root, True, 9)]
    assert result.count == 1
    assert result.entries[0].type == "file"
    assert result.entries[0].size == 3
