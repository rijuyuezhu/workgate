import base64
import ctypes
import hashlib
import io
import os
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import workgate.ops.transfer as transfer_ops
import workgate.remote_worker.http_transfer as worker_http_transfer
import workgate.tools.registry.transfer as transfer_registry
from workgate.config.settings import clear_settings_cache
from workgate.control.mcp.app import build_mcp
from workgate.ops.transfer import (
    transfer_abort_write,
    transfer_alloc_temp_path,
    transfer_begin_write,
    transfer_finish_write,
    transfer_pack_dir,
    transfer_read_chunk,
    transfer_stat,
    transfer_unpack_archive,
    transfer_write_chunk,
)
from workgate.ops.utils.path import temp_dir


def _workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".workgate"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    return tmp_path


def test_transfer_handle_identity_uses_platform_native_ids(
    tmp_path, monkeypatch
):
    path = tmp_path / "identity.bin"
    path.write_bytes(b"data")

    with path.open("rb") as handle:
        file_stat = os.fstat(handle.fileno())
        assert transfer_ops._transfer_handle_identity(
            handle, platform="posix"
        ) == (int(file_stat.st_dev), int(file_stat.st_ino))

        seen_descriptors = []
        monkeypatch.setattr(
            transfer_ops,
            "_windows_file_identity",
            lambda descriptor: seen_descriptors.append(descriptor) or (7, 11),
        )
        assert transfer_ops._transfer_handle_identity(
            handle, platform="nt"
        ) == (7, 11)
        assert seen_descriptors == [handle.fileno()]


class _FakeWindowsFunction:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.argtypes: Any = None
        self.restype: Any = None

    def __call__(self, *args):
        return self.callback(*args)


def test_windows_transfer_opener_rejects_reparse_and_closes_failures(
    tmp_path, monkeypatch
):
    path = tmp_path / "windows-open.bin"
    path.write_bytes(b"data")
    backing_fd = os.open(path, os.O_RDWR)
    native_handle = 1234
    attributes = 0
    information_ok = True
    create_result = native_handle
    close_calls: list[int] = []
    open_calls: list[int] = []

    def create_file(*_args):
        return create_result

    def get_information(handle, pointer):
        assert int(handle.value) == native_handle
        if not information_ok:
            return 0
        information = ctypes.cast(
            pointer,
            ctypes.POINTER(transfer_ops._ByHandleFileInformation),
        ).contents
        information.dwFileAttributes = attributes
        information.dwVolumeSerialNumber = 7
        information.nFileIndexHigh = 0
        information.nFileIndexLow = 11
        return 1

    def close_handle(handle):
        close_calls.append(int(handle.value))
        return 1

    kernel32 = SimpleNamespace(
        CreateFileW=_FakeWindowsFunction(create_file),
        GetFileInformationByHandle=_FakeWindowsFunction(get_information),
        CloseHandle=_FakeWindowsFunction(close_handle),
    )
    monkeypatch.setattr(
        transfer_ops.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: kernel32,
        raising=False,
    )
    monkeypatch.setattr(
        transfer_ops.ctypes, "get_last_error", lambda: 5, raising=False
    )
    real_import_module = transfer_ops.importlib.import_module

    def import_module(name: str):
        if name != "msvcrt":
            return real_import_module(name)

        def open_osfhandle(handle: int, _flags: int) -> int:
            open_calls.append(handle)
            return os.dup(backing_fd)

        return SimpleNamespace(open_osfhandle=open_osfhandle)

    monkeypatch.setattr(transfer_ops.importlib, "import_module", import_module)
    try:
        with transfer_ops._open_windows_transfer_file(
            path, update=False
        ) as source:
            assert source.read() == b"data"
        with transfer_ops._open_windows_transfer_file(
            path, update=True
        ) as destination:
            destination.seek(0)
            destination.write(b"DATA")
        assert path.read_bytes() == b"DATA"
        assert open_calls == [native_handle, native_handle]

        attributes = 0x00000400
        with pytest.raises(ValueError, match="reparse point"):
            transfer_ops._open_windows_transfer_file(path, update=False)
        assert close_calls == [native_handle]

        attributes = 0
        information_ok = False
        with pytest.raises(OSError, match="GetFileInformationByHandle"):
            transfer_ops._open_windows_transfer_file(path, update=False)
        assert close_calls == [native_handle, native_handle]

        information_ok = True
        invalid_handle = transfer_ops.wintypes.HANDLE(-1).value
        create_result = invalid_handle
        with pytest.raises(OSError, match="CreateFileW"):
            transfer_ops._open_windows_transfer_file(path, update=False)
    finally:
        os.close(backing_fd)


def test_chunked_transfer_round_trip_and_checksum(tmp_path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    data = bytes(range(256)) * 3000 + b"tail"
    (root / "source.bin").write_bytes(data)

    stat = transfer_stat("source.bin", sha256=True)
    assert stat.size is not None
    begin = transfer_begin_write(
        "nested/dest.bin", overwrite=True, expected_bytes=stat.size
    )

    offset = 0
    chunks = 0
    while offset < stat.size:
        chunk = transfer_read_chunk(
            "source.bin", offset=offset, chunk_size=10_000
        )
        transfer_write_chunk(
            "nested/dest.bin",
            begin.transfer_id,
            offset,
            chunk.data_b64,
            chunk.sha256,
        )
        offset += chunk.bytes
        chunks += 1

    finish = transfer_finish_write(
        "nested/dest.bin",
        begin.transfer_id,
        expected_bytes=stat.size,
        expected_sha256=stat.sha256,
    )

    assert chunks > 1
    assert finish.bytes == len(data)
    assert finish.sha256 == stat.sha256
    assert (root / "nested" / "dest.bin").read_bytes() == data


def test_transfer_rejects_bad_chunk_checksum_and_abort_removes_temp(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    (root / "source.txt").write_text("hello", encoding="utf-8")
    begin = transfer_begin_write("dest.txt", overwrite=True, expected_bytes=5)
    chunk = transfer_read_chunk("source.txt", offset=0, chunk_size=128)

    with pytest.raises(ValueError, match="chunk sha256 mismatch"):
        transfer_write_chunk(
            "dest.txt", begin.transfer_id, 0, chunk.data_b64, "0" * 64
        )

    abort = transfer_abort_write("dest.txt", begin.transfer_id)
    assert abort.deleted is True
    assert not any(root.glob(".dest.txt.workgate-transfer-*.tmp"))
    assert not (root / "dest.txt").exists()


def test_directory_pack_and_unpack_preserves_nested_files(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    (root / "src" / "sub").mkdir(parents=True)
    (root / "src" / "sub" / "file.txt").write_text("nested", encoding="utf-8")
    (root / "src" / "root.bin").write_bytes(b"\x00\x01")

    pack = transfer_pack_dir("src")
    unpack = transfer_unpack_archive(pack.archive_path, "dst", overwrite=True)

    assert unpack.entries >= 2
    assert (root / "dst" / "sub" / "file.txt").read_text(
        encoding="utf-8"
    ) == "nested"
    assert (root / "dst" / "root.bin").read_bytes() == b"\x00\x01"
    assert not (root / pack.archive_path).exists()


def test_directory_pack_rejects_symlink_members(tmp_path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    (root / "src").mkdir()
    (root / "target.txt").write_text("target", encoding="utf-8")
    try:
        (root / "src" / "link.txt").symlink_to(root / "target.txt")
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(ValueError, match="symlink"):
        transfer_pack_dir("src")


def test_unpack_rejects_archive_path_traversal(tmp_path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    archive = root / "bad.tar"
    payload = b"bad"
    info = tarfile.TarInfo("../escape.txt")
    info.size = len(payload)
    with tarfile.open(archive, "w") as tar:
        tar.addfile(info, io.BytesIO(payload))

    with pytest.raises(ValueError, match="unsafe archive member path"):
        transfer_unpack_archive(
            "bad.tar", "dst", overwrite=True, cleanup_archive=False
        )

    assert not (root.parent / "escape.txt").exists()


@pytest.mark.asyncio
async def test_mcp_does_not_expose_remote_transfer_tools(tmp_path, monkeypatch):
    _workspace(tmp_path, monkeypatch)
    tools = {tool.name: tool for tool in await build_mcp().list_tools()}

    assert "remote" not in tools
    assert {
        "remote_copy_file",
        "remote_copy_dir",
        "remote_pull_file",
        "remote_push_file",
        "remote_pull_dir",
        "remote_push_dir",
    }.isdisjoint(tools)


@pytest.mark.asyncio
async def test_registered_transfer_handlers_reach_local_worker_clients(
    tmp_path, monkeypatch
):
    _workspace(tmp_path, monkeypatch)
    begin = transfer_begin_write("abort.bin", expected_bytes=1)
    aborted = await transfer_registry.transfer_abort_write.func(
        "abort.bin", begin.transfer_id
    )
    assert aborted.deleted is True

    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_upload_file(**kwargs):
        calls.append(("upload", kwargs))
        return {"ok": True, "direction": "upload"}

    def fake_download_file(**kwargs):
        calls.append(("download", kwargs))
        return {"ok": True, "direction": "download"}

    def fake_abort_download(**kwargs):
        calls.append(("abort", kwargs))
        return {"ok": True, "direction": "abort"}

    monkeypatch.setattr(worker_http_transfer, "upload_file", fake_upload_file)
    monkeypatch.setattr(
        worker_http_transfer, "download_file", fake_download_file
    )
    monkeypatch.setattr(
        worker_http_transfer, "abort_download", fake_abort_download
    )

    common = {
        "path": "payload.bin",
        "session_id": "SESSION1",
        "url": "https://worker.example/transfer",
        "controller_url": "https://controller.example",
        "authorization": "Bearer token",
        "worker": "worker-a",
        "expected_bytes": 7,
        "expected_sha256": "a" * 64,
        "chunk_size": 1024,
        "timeout_s": 5.0,
    }
    assert await transfer_registry.transfer_http_upload.func(**common) == {
        "ok": True,
        "direction": "upload",
    }
    assert await transfer_registry.transfer_http_download.func(
        **common,
        transfer_id="transfer-1",
        overwrite=False,
    ) == {"ok": True, "direction": "download"}
    assert await transfer_registry.transfer_http_abort_download.func(
        path="payload.bin",
        session_id="SESSION1",
        transfer_id="transfer-1",
    ) == {"ok": True, "direction": "abort"}

    assert [kind for kind, _ in calls] == ["upload", "download", "abort"]
    assert calls[0][1]["expected_sha256"] == "a" * 64
    assert calls[1][1]["transfer_id"] == "transfer-1"
    assert calls[1][1]["overwrite"] is False
    assert calls[2][1] == {
        "path": "payload.bin",
        "session_id": "SESSION1",
        "transfer_id": "transfer-1",
    }


def _write_payload(
    path: str, transfer_id: str, offset: int, payload: bytes
) -> None:
    transfer_write_chunk(
        path,
        transfer_id,
        offset,
        base64.b64encode(payload).decode("ascii"),
        hashlib.sha256(payload).hexdigest(),
    )


def test_finish_rechecks_overwrite_false_after_begin(tmp_path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    begin = transfer_begin_write("dest.txt", overwrite=False, expected_bytes=3)
    _write_payload("dest.txt", begin.transfer_id, 0, b"new")
    (root / "dest.txt").write_text("racer", encoding="utf-8")

    with pytest.raises(FileExistsError):
        transfer_finish_write(
            "dest.txt",
            begin.transfer_id,
            expected_bytes=3,
            expected_sha256=hashlib.sha256(b"new").hexdigest(),
        )

    assert (root / "dest.txt").read_text(encoding="utf-8") == "racer"
    assert transfer_abort_write("dest.txt", begin.transfer_id).deleted is True


def test_transfer_rejects_overlapping_and_missing_ranges(tmp_path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    begin = transfer_begin_write("dest.bin", expected_bytes=4)
    _write_payload("dest.bin", begin.transfer_id, 0, b"ab")

    with pytest.raises(ValueError, match="overlaps"):
        _write_payload("dest.bin", begin.transfer_id, 1, b"bc")

    transfer_abort_write("dest.bin", begin.transfer_id)
    begin = transfer_begin_write("dest.bin", expected_bytes=4)
    _write_payload("dest.bin", begin.transfer_id, 2, b"cd")

    with pytest.raises(ValueError, match="missing or non-contiguous"):
        transfer_finish_write("dest.bin", begin.transfer_id, expected_bytes=4)

    assert not (root / "dest.bin").exists()
    transfer_abort_write("dest.bin", begin.transfer_id)


def test_chunk_cannot_exceed_declared_transfer_size(tmp_path, monkeypatch):
    _workspace(tmp_path, monkeypatch)
    begin = transfer_begin_write("dest.bin", expected_bytes=2)

    with pytest.raises(ValueError, match="exceeds expected transfer size"):
        _write_payload("dest.bin", begin.transfer_id, 0, b"three")

    transfer_abort_write("dest.bin", begin.transfer_id)


def test_transfer_id_and_private_state_are_hardened(tmp_path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    begin = transfer_begin_write("dest.bin", expected_bytes=1)
    temporary = root / begin.temp_path
    metadata = temporary.with_name(temporary.name + ".json")

    if os.name != "nt":
        assert temporary.stat().st_mode & 0o777 == 0o600
        assert metadata.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="unsupported characters"):
        transfer_abort_write("dest.bin", "../invalid")

    transfer_abort_write("dest.bin", begin.transfer_id)
    assert not temporary.exists()
    assert not metadata.exists()


def test_replaced_transfer_temp_is_rejected(tmp_path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    begin = transfer_begin_write("dest.bin", expected_bytes=3)
    temporary = root / begin.temp_path
    replacement = temporary.with_name(temporary.name + ".replacement")
    replacement.write_bytes(b"")
    os.replace(replacement, temporary)

    with pytest.raises(ValueError, match="identity changed"):
        _write_payload("dest.bin", begin.transfer_id, 0, b"new")

    transfer_abort_write("dest.bin", begin.transfer_id)


def test_externally_resized_transfer_temp_is_rejected(tmp_path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    begin = transfer_begin_write("dest.bin", expected_bytes=3)
    temporary = root / begin.temp_path
    temporary.write_bytes(b"x")

    with pytest.raises(ValueError, match="size changed"):
        _write_payload("dest.bin", begin.transfer_id, 0, b"new")

    transfer_abort_write("dest.bin", begin.transfer_id)


def test_finish_replaces_final_symlink_not_its_target(tmp_path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    target = root / "target.txt"
    target.write_text("old", encoding="utf-8")
    link = root / "dest.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    begin = transfer_begin_write("dest.txt", overwrite=True, expected_bytes=3)
    _write_payload("dest.txt", begin.transfer_id, 0, b"new")
    transfer_finish_write(
        "dest.txt",
        begin.transfer_id,
        expected_bytes=3,
        expected_sha256=hashlib.sha256(b"new").hexdigest(),
    )

    assert not link.is_symlink()
    assert link.read_text(encoding="utf-8") == "new"
    assert target.read_text(encoding="utf-8") == "old"


def test_begin_prunes_only_stale_destination_transfers(tmp_path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    stale = root / ".dest.bin.workgate-transfer-stale.tmp"
    stale_metadata = stale.with_name(stale.name + ".json")
    orphan_metadata = root / (".dest.bin.workgate-transfer-orphan.tmp.json")
    recent = root / ".dest.bin.workgate-transfer-recent.tmp"
    stale.write_bytes(b"stale")
    stale_metadata.write_text("{}", encoding="utf-8")
    orphan_metadata.write_text("{}", encoding="utf-8")
    recent.write_bytes(b"recent")
    old = time.time() - 2 * 24 * 60 * 60
    os.utime(stale, (old, old))
    os.utime(stale_metadata, (old, old))
    os.utime(orphan_metadata, (old, old))

    begin = transfer_begin_write("dest.bin", expected_bytes=0)

    assert not stale.exists()
    assert not stale_metadata.exists()
    assert not orphan_metadata.exists()
    assert recent.read_bytes() == b"recent"
    transfer_abort_write("dest.bin", begin.transfer_id)
    recent.unlink()


def _archive_with_files(path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def test_unpack_limits_preserve_existing_destination(tmp_path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    destination = root / "dst"
    destination.mkdir()
    (destination / "important.txt").write_text("keep", encoding="utf-8")
    archive = root / "large.tar"
    _archive_with_files(archive, {"payload.bin": b"1234"})
    monkeypatch.setenv("WORKGATE_MAX_TRANSFER_UNPACKED_BYTES", "3")
    clear_settings_cache()

    with pytest.raises(ValueError, match="expands to more than 3 bytes"):
        transfer_unpack_archive(
            "large.tar", "dst", overwrite=True, cleanup_archive=False
        )

    assert (destination / "important.txt").read_text(encoding="utf-8") == "keep"
    assert archive.exists()
    assert not list(root.glob(".dst.unpack-*"))


def test_unpack_entry_limit_preserves_existing_destination(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    destination = root / "dst"
    destination.mkdir()
    (destination / "important.txt").write_text("keep", encoding="utf-8")
    archive = root / "many.tar"
    _archive_with_files(archive, {"one.txt": b"1", "two.txt": b"2"})
    monkeypatch.setenv("WORKGATE_MAX_TRANSFER_ARCHIVE_ENTRIES", "1")
    clear_settings_cache()

    with pytest.raises(ValueError, match="more than 1 entries"):
        transfer_unpack_archive(
            "many.tar", "dst", overwrite=True, cleanup_archive=False
        )

    assert (destination / "important.txt").read_text(encoding="utf-8") == "keep"


def test_unpack_commits_before_reporting_backup_cleanup_failure(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    destination = root / "dst"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    archive = root / "new.tar"
    _archive_with_files(archive, {"new.txt": b"new"})
    original_remove = transfer_ops._remove_existing_path

    def fail_backup_cleanup(path):
        if ".backup-" in path.name:
            raise OSError("simulated cleanup failure")
        original_remove(path)

    monkeypatch.setattr(
        transfer_ops, "_remove_existing_path", fail_backup_cleanup
    )

    result = transfer_unpack_archive(
        "new.tar", "dst", overwrite=True, cleanup_archive=True
    )

    assert result.completed is True
    assert result.backup_deleted is False
    assert (
        result.cleanup_errors
        and "simulated cleanup failure" in result.cleanup_errors[0]
    )
    assert (destination / "new.txt").read_bytes() == b"new"
    assert not archive.exists()
    backups = list(root.glob(".dst.backup-*"))
    assert len(backups) == 1
    original_remove(backups[0])


def test_transfer_temp_pruning_preserves_recent_active_files(
    tmp_path, monkeypatch
):
    _workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("WORKGATE_MAX_TMP_FILES", "0")
    monkeypatch.setenv("WORKGATE_MAX_TMP_BYTES", "0")
    clear_settings_cache()
    directory = temp_dir()
    stale = directory / "stale.bin"
    recent = directory / "recent.bin"
    stale.write_bytes(b"stale")
    recent.write_bytes(b"recent")
    old = time.time() - 2 * 24 * 60 * 60
    os.utime(stale, (old, old))

    transfer_alloc_temp_path(".bin")

    assert not stale.exists()
    assert recent.read_bytes() == b"recent"


def test_unpack_commit_failure_restores_previous_destination(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    destination = root / "dst"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    archive = root / "new.tar"
    _archive_with_files(archive, {"new.txt": b"new"})
    original_replace = transfer_ops.os.replace

    def fail_staging_commit(source, target):
        if ".dst.unpack-" in str(source) and Path(target) == destination:
            raise OSError("simulated commit failure")
        return original_replace(source, target)

    monkeypatch.setattr(transfer_ops.os, "replace", fail_staging_commit)

    with pytest.raises(OSError, match="simulated commit failure"):
        transfer_unpack_archive(
            "new.tar", "dst", overwrite=True, cleanup_archive=True
        )

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (destination / "new.txt").exists()
    assert archive.exists()
    assert not list(root.glob(".dst.unpack-*"))
    assert not list(root.glob(".dst.backup-*"))
