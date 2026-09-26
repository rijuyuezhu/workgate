import base64
import ctypes
import hashlib
import io
import os
import shutil
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import workgate.executor.transfer as transfer_ops
from tests.helpers import get_test_tool_session_store as get_tool_session_store
from workgate.config.executor import resolve_executor_config
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.mcp.app import build_mcp
from workgate.executor.path import temp_dir as _executor_temp_dir
from workgate.executor.transfer import TransferContext

_TRANSFER_CONTEXT: TransferContext | None = None


def _context() -> TransferContext:
    assert _TRANSFER_CONTEXT is not None
    return _TRANSFER_CONTEXT


def _refresh_context() -> None:
    global _TRANSFER_CONTEXT
    current = _context()
    _TRANSFER_CONTEXT = TransferContext(
        resolve_executor_config(get_settings()), current.store
    )


def _workspace(tmp_path, monkeypatch):
    global _TRANSFER_CONTEXT
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".workgate"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()
    store = get_tool_session_store()
    store.clear()
    _TRANSFER_CONTEXT = TransferContext(
        resolve_executor_config(get_settings()), store
    )
    return tmp_path


def transfer_stat(*args, **kwargs):
    return transfer_ops.transfer_stat(*args, context=_context(), **kwargs)


def transfer_read_chunk(*args, **kwargs):
    return transfer_ops.transfer_read_chunk(*args, context=_context(), **kwargs)


def transfer_begin_write(*args, **kwargs):
    return transfer_ops.transfer_begin_write(
        *args, context=_context(), **kwargs
    )


def transfer_write_chunk(*args, **kwargs):
    return transfer_ops.transfer_write_chunk(
        *args, context=_context(), **kwargs
    )


def transfer_finish_write(*args, **kwargs):
    return transfer_ops.transfer_finish_write(
        *args, context=_context(), **kwargs
    )


def transfer_abort_write(*args, **kwargs):
    return transfer_ops.transfer_abort_write(
        *args, context=_context(), **kwargs
    )


def transfer_alloc_temp_path(*args, **kwargs):
    return transfer_ops.transfer_alloc_temp_path(
        *args, context=_context(), **kwargs
    )


def transfer_pack_dir(*args, **kwargs):
    return transfer_ops.transfer_pack_dir(*args, context=_context(), **kwargs)


def transfer_unpack_archive(*args, **kwargs):
    return transfer_ops.transfer_unpack_archive(
        *args, context=_context(), **kwargs
    )


def temp_dir():
    return _executor_temp_dir(_context().config.temp_dir)


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


def test_begin_does_not_prune_stale_write_owned_by_live_receipt(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    old_id = "owned-stale"
    transfer_begin_write("dest.bin", expected_bytes=4, transfer_id=old_id)
    transfer_ops.transfer_write_bytes(
        "dest.bin", old_id, 0, b"data", context=context
    )
    destination = root / "dest.bin"
    temporary = transfer_ops._transfer_temp_path(destination, old_id)
    metadata = transfer_ops._transfer_metadata_path(temporary)
    receipt_path = transfer_ops._write_receipt_path(context, old_id)
    transfer_ops._write_write_receipt(
        context,
        receipt_path,
        {
            "destination": str(destination),
            "overwrite": True,
            "expected_bytes": 4,
            "destination_existed": False,
            "status": "committing",
            "final_bytes": 4,
            "final_sha256": hashlib.sha256(b"data").hexdigest(),
            "updated_at": time.time(),
        },
    )
    old = time.time() - 2 * 24 * 60 * 60
    os.utime(temporary, (old, old))
    os.utime(metadata, (old, old))

    newer = transfer_begin_write(
        "dest.bin", expected_bytes=1, transfer_id="new-write"
    )

    assert temporary.exists()
    assert metadata.exists()
    resumed = transfer_begin_write(
        "dest.bin", expected_bytes=4, transfer_id=old_id
    )
    assert resumed.resumed is True
    assert resumed.offset == 4
    transfer_abort_write("dest.bin", newer.transfer_id)


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
    _refresh_context()

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
    _refresh_context()

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
    _refresh_context()
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


def test_transfer_temp_pruning_tolerates_unlink_failure(tmp_path, monkeypatch):
    _workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("WORKGATE_MAX_TMP_FILES", "0")
    monkeypatch.setenv("WORKGATE_MAX_TMP_BYTES", "0")
    clear_settings_cache()
    _refresh_context()
    stale = temp_dir() / "stale.bin"
    stale.write_bytes(b"stale")
    old = time.time() - 2 * 24 * 60 * 60
    os.utime(stale, (old, old))
    real_unlink = Path.unlink

    def fail_stale_unlink(path, *args, **kwargs):
        if path == stale:
            raise OSError("simulated unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_stale_unlink)
    transfer_alloc_temp_path(".bin")

    assert stale.read_bytes() == b"stale"


def test_transfer_temp_pruning_fails_closed_on_invalid_receipt(
    tmp_path, monkeypatch
):
    _workspace(tmp_path, monkeypatch)
    context = _context()
    invalid_receipt = transfer_ops._write_receipt_path(
        context, "invalid-gc-receipt"
    )
    context.store.state_store.write_json(
        invalid_receipt, {"status": "receiving"}
    )
    stale = temp_dir() / "stale.bin"
    stale.write_bytes(b"stale")
    old = time.time() - 2 * 24 * 60 * 60
    os.utime(stale, (old, old))
    monkeypatch.setenv("WORKGATE_MAX_TMP_FILES", "0")
    monkeypatch.setenv("WORKGATE_MAX_TMP_BYTES", "0")
    clear_settings_cache()
    _refresh_context()

    transfer_alloc_temp_path(".bin")

    assert stale.read_bytes() == b"stale"


def test_transfer_temp_pruning_fails_closed_on_invalid_unpack_receipt(
    tmp_path, monkeypatch
):
    _workspace(tmp_path, monkeypatch)
    context = _context()
    invalid_receipt = transfer_ops._unpack_receipt_path(
        context, "invalid-gc-unpack-receipt"
    )
    context.store.state_store.write_json(
        invalid_receipt, {"status": "extracting"}
    )
    stale = temp_dir() / "stale.bin"
    stale.write_bytes(b"stale")
    old = time.time() - 2 * 24 * 60 * 60
    os.utime(stale, (old, old))
    monkeypatch.setenv("WORKGATE_MAX_TMP_FILES", "0")
    monkeypatch.setenv("WORKGATE_MAX_TMP_BYTES", "0")
    clear_settings_cache()
    _refresh_context()

    transfer_alloc_temp_path(".bin")

    assert stale.read_bytes() == b"stale"


def test_temp_gc_preserves_scratch_archive_owned_by_live_receipt(
    tmp_path, monkeypatch
):
    _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "gc-owned-archive"
    scratch = transfer_alloc_temp_path(".tar")
    transfer_begin_write(
        scratch.path,
        expected_bytes=7,
        transfer_id=transfer_id,
    )
    transfer_ops.transfer_write_bytes(
        scratch.path, transfer_id, 0, b"archive", context=context
    )
    transfer_finish_write(
        scratch.path,
        transfer_id,
        expected_bytes=7,
        expected_sha256=hashlib.sha256(b"archive").hexdigest(),
    )
    archive = transfer_ops._resolve_temp_path(scratch.path, context=context)
    old = time.time() - 2 * 24 * 60 * 60
    os.utime(archive, (old, old))

    monkeypatch.setenv("WORKGATE_MAX_TMP_FILES", "0")
    monkeypatch.setenv("WORKGATE_MAX_TMP_BYTES", "0")
    clear_settings_cache()
    _refresh_context()
    context = _context()
    unrelated = temp_dir() / "unrelated-stale.bin"
    unrelated.write_bytes(b"stale")
    os.utime(unrelated, (old, old))

    retried = transfer_begin_write(
        scratch.path,
        expected_bytes=7,
        transfer_id=transfer_id,
    )

    assert retried.completed is True
    assert archive.read_bytes() == b"archive"
    assert not unrelated.exists()
    transfer_ops.prune_temp_dir(
        max_files=0,
        max_bytes=0,
        minimum_age_s=0,
        directory=context.config.temp_dir,
    )
    assert archive.read_bytes() == b"archive"
    assert transfer_ops._write_receipt_path(context, transfer_id).exists()


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


def test_explicit_workdir_transfer_resolution_is_bounded(tmp_path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    workdir = root / "session"
    workdir.mkdir()
    (workdir / "file.txt").write_text("payload", encoding="utf-8")
    (root / "outside.txt").write_text("outside", encoding="utf-8")

    stat = transfer_ops.transfer_stat(
        "file.txt", workdir=str(workdir), context=_context()
    )
    assert Path(stat.path) == Path("session") / "file.txt"

    with pytest.raises(ValueError, match="mutually exclusive"):
        transfer_ops.transfer_stat(
            "file.txt",
            session_id="sess-unused",
            workdir=str(workdir),
            context=_context(),
        )
    with pytest.raises(ValueError, match="escapes session workdir"):
        transfer_ops.transfer_stat(
            "../outside.txt", workdir=str(workdir), context=_context()
        )


def test_transfer_read_chunk_rejects_invalid_source_ranges(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    (root / "dir").mkdir()
    (root / "file.bin").write_bytes(b"abc")

    with pytest.raises(IsADirectoryError):
        transfer_read_chunk("dir")
    with pytest.raises(ValueError, match="offset must be >= 0"):
        transfer_read_chunk("file.bin", offset=-1)
    with pytest.raises(ValueError, match="exceeds source size"):
        transfer_read_chunk("file.bin", offset=4)


def test_transfer_copy_file_handles_same_path_and_destination_conflicts(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    source = root / "source.bin"
    source.write_bytes(b"payload")

    same = transfer_ops.transfer_copy_file(
        "source.bin", "source.bin", context=_context()
    )
    assert same.completed is True
    assert same.bytes == len(b"payload")
    assert same.chunks == 1

    with pytest.raises(FileExistsError):
        transfer_ops.transfer_copy_file(
            "source.bin", "source.bin", overwrite=False, context=_context()
        )

    copied = transfer_ops.transfer_copy_file(
        "source.bin", "nested/dest.bin", chunk_size=3, context=_context()
    )
    assert copied.completed is True
    assert copied.chunks == 3
    assert (root / "nested" / "dest.bin").read_bytes() == b"payload"

    with pytest.raises(FileExistsError):
        transfer_ops.transfer_copy_file(
            "source.bin", "nested/dest.bin", overwrite=False, context=_context()
        )
    (root / "directory-dest").mkdir()
    with pytest.raises(IsADirectoryError):
        transfer_ops.transfer_copy_file(
            "source.bin", "directory-dest", context=_context()
        )


def test_transfer_begin_resume_validates_transaction_contract(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    transfer_id = "resume-1"
    transfer_ops.transfer_begin_write(
        "dest.bin",
        expected_bytes=4,
        transfer_id=transfer_id,
        context=_context(),
    )
    transfer_ops.transfer_write_bytes(
        "dest.bin", transfer_id, 0, b"ab", context=_context()
    )

    resumed = transfer_ops.transfer_begin_write(
        "dest.bin",
        expected_bytes=4,
        transfer_id=transfer_id,
        context=_context(),
    )
    assert resumed.resumed is True
    assert resumed.offset == 2

    with pytest.raises(ValueError, match="overwrite mode mismatch"):
        transfer_ops.transfer_begin_write(
            "dest.bin",
            overwrite=False,
            expected_bytes=4,
            transfer_id=transfer_id,
            context=_context(),
        )
    with pytest.raises(ValueError, match="expected size mismatch"):
        transfer_ops.transfer_begin_write(
            "dest.bin",
            expected_bytes=5,
            transfer_id=transfer_id,
            context=_context(),
        )
    with pytest.raises(ValueError, match="expected_bytes must be >= 0"):
        transfer_ops.transfer_begin_write(
            "negative.bin", expected_bytes=-1, context=_context()
        )

    transfer_abort_write("dest.bin", transfer_id)
    (root / "existing.bin").write_bytes(b"old")
    with pytest.raises(FileExistsError):
        transfer_ops.transfer_begin_write(
            "existing.bin", overwrite=False, context=_context()
        )
    (root / "directory-dest").mkdir()
    with pytest.raises(IsADirectoryError):
        transfer_ops.transfer_begin_write("directory-dest", context=_context())


def test_transfer_begin_rolls_back_uncommitted_suffix_after_chunk_crash(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "chunk-crash-before-metadata"
    transfer_begin_write("dest.bin", expected_bytes=4, transfer_id=transfer_id)
    real_write_metadata = transfer_ops._write_transfer_metadata
    crashed = False

    def crash_before_metadata_commit(temporary, metadata):
        nonlocal crashed
        if not crashed and metadata.get("received_ranges") == [[0, 2]]:
            crashed = True
            raise OSError("simulated crash before chunk metadata commit")
        real_write_metadata(temporary, metadata)

    monkeypatch.setattr(
        transfer_ops, "_write_transfer_metadata", crash_before_metadata_commit
    )
    with pytest.raises(OSError, match="chunk metadata commit"):
        transfer_ops.transfer_write_bytes(
            "dest.bin", transfer_id, 0, b"ab", context=context
        )

    temporary = transfer_ops._transfer_temp_path(root / "dest.bin", transfer_id)
    assert temporary.read_bytes() == b"ab"
    monkeypatch.setattr(
        transfer_ops, "_write_transfer_metadata", real_write_metadata
    )

    resumed = transfer_begin_write(
        "dest.bin", expected_bytes=4, transfer_id=transfer_id
    )
    assert resumed.resumed is True
    assert resumed.offset == 0
    assert temporary.read_bytes() == b""

    transfer_ops.transfer_write_bytes(
        "dest.bin", transfer_id, 0, b"data", context=context
    )
    finished = transfer_finish_write(
        "dest.bin",
        transfer_id,
        expected_bytes=4,
        expected_sha256=hashlib.sha256(b"data").hexdigest(),
    )
    assert finished.completed is True
    assert (root / "dest.bin").read_bytes() == b"data"


def test_transfer_begin_does_not_recover_missing_durable_prefix(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    transfer_id = "missing-durable-prefix"
    transfer_begin_write("dest.bin", expected_bytes=4, transfer_id=transfer_id)
    transfer_ops.transfer_write_bytes(
        "dest.bin", transfer_id, 0, b"ab", context=_context()
    )
    temporary = transfer_ops._transfer_temp_path(root / "dest.bin", transfer_id)
    with temporary.open("r+b") as handle:
        handle.truncate(1)

    with pytest.raises(ValueError, match="temporary file size changed"):
        transfer_begin_write(
            "dest.bin", expected_bytes=4, transfer_id=transfer_id
        )


def test_transfer_begin_recovers_commit_after_lost_finish_response(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    transfer_id = "commit-receipt-1"
    begin = transfer_begin_write(
        "dest.bin",
        overwrite=False,
        expected_bytes=4,
        transfer_id=transfer_id,
    )
    transfer_ops.transfer_write_bytes(
        "dest.bin",
        begin.transfer_id,
        0,
        b"data",
        context=_context(),
    )
    real_write_receipt = transfer_ops._write_write_receipt

    def lose_completed_receipt(context, path, receipt):
        if receipt.get("status") == "completed":
            raise OSError("simulated lost finish acknowledgement")
        real_write_receipt(context, path, receipt)

    monkeypatch.setattr(
        transfer_ops, "_write_write_receipt", lose_completed_receipt
    )
    with pytest.raises(OSError, match="lost finish acknowledgement"):
        transfer_finish_write(
            "dest.bin",
            begin.transfer_id,
            expected_bytes=4,
            expected_sha256=hashlib.sha256(b"data").hexdigest(),
        )

    assert (root / "dest.bin").read_bytes() == b"data"
    monkeypatch.setattr(
        transfer_ops, "_write_write_receipt", real_write_receipt
    )
    recovered = transfer_begin_write(
        "dest.bin",
        overwrite=False,
        expected_bytes=4,
        transfer_id=transfer_id,
    )
    assert recovered.completed is True
    assert recovered.resumed is True
    assert recovered.offset == 4
    assert recovered.sha256 == hashlib.sha256(b"data").hexdigest()
    receipt = _context().store.state_store.read_json(
        transfer_ops._write_receipt_path(_context(), transfer_id)
    )
    assert receipt is not None
    assert receipt["status"] == "completed"


def test_recovered_write_receipt_survives_unpack_consuming_scratch_archive(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "lost-write-completion-before-unpack"
    source_archive = root / "payload.tar"
    _archive_with_files(source_archive, {"note.txt": b"hello"})
    payload = source_archive.read_bytes()
    scratch = transfer_alloc_temp_path(".tar")
    transfer_begin_write(
        scratch.path,
        expected_bytes=len(payload),
        transfer_id=transfer_id,
    )
    transfer_ops.transfer_write_bytes(
        scratch.path, transfer_id, 0, payload, context=context
    )
    real_write_receipt = transfer_ops._write_write_receipt

    def lose_completed_receipt(context, path, receipt):
        if receipt.get("status") == "completed":
            raise OSError("simulated lost completed write receipt")
        real_write_receipt(context, path, receipt)

    monkeypatch.setattr(
        transfer_ops, "_write_write_receipt", lose_completed_receipt
    )
    with pytest.raises(OSError, match="lost completed write receipt"):
        transfer_finish_write(
            scratch.path,
            transfer_id,
            expected_bytes=len(payload),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )
    receipt_path = transfer_ops._write_receipt_path(context, transfer_id)
    assert (
        transfer_ops._load_write_receipt(context, receipt_path).status
        == "committing"
    )

    monkeypatch.setattr(
        transfer_ops, "_write_write_receipt", real_write_receipt
    )
    recovered = transfer_begin_write(
        scratch.path,
        expected_bytes=len(payload),
        transfer_id=transfer_id,
    )
    assert recovered.completed is True
    assert (
        transfer_ops._load_write_receipt(context, receipt_path).status
        == "completed"
    )

    unpacked = transfer_unpack_archive(
        scratch.path,
        "dest",
        overwrite=True,
        cleanup_archive=True,
        transfer_id=transfer_id,
    )
    assert unpacked.completed is True
    assert (root / "dest" / "note.txt").read_bytes() == b"hello"
    archive = transfer_ops._resolve_temp_path(scratch.path, context=context)
    assert not archive.exists()
    _refresh_context()
    context = _context()

    retried = transfer_begin_write(
        scratch.path,
        expected_bytes=len(payload),
        transfer_id=transfer_id,
    )
    assert retried.completed is True
    abandoned = transfer_ops.transfer_abandon_import(
        transfer_id, "dir", context=context
    )
    assert abandoned["safe_to_forget"] is True


def test_unpack_transfer_receipt_recovers_atomic_directory_commit(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    source = root / "source-dir"
    source.mkdir()
    (source / "note.txt").write_text("hello", encoding="utf-8")
    packed = transfer_pack_dir("source-dir", compression="gz")
    real_write_receipt = transfer_ops._write_unpack_receipt

    def lose_completed_receipt(context, path, receipt):
        if receipt.get("status") == "completed":
            raise OSError("simulated lost unpack acknowledgement")
        real_write_receipt(context, path, receipt)

    monkeypatch.setattr(
        transfer_ops, "_write_unpack_receipt", lose_completed_receipt
    )
    with pytest.raises(OSError, match="lost unpack acknowledgement"):
        transfer_unpack_archive(
            packed.archive_path,
            "dest-dir",
            overwrite=False,
            cleanup_archive=True,
            transfer_id="unpack-receipt-1",
        )

    assert (root / "dest-dir" / "note.txt").read_text(
        encoding="utf-8"
    ) == "hello"
    monkeypatch.setattr(
        transfer_ops, "_write_unpack_receipt", real_write_receipt
    )
    recovered = transfer_unpack_archive(
        packed.archive_path,
        "dest-dir",
        overwrite=False,
        cleanup_archive=True,
        transfer_id="unpack-receipt-1",
    )
    assert recovered.completed is True
    assert recovered.resumed is True
    assert recovered.entries >= 1


def test_transfer_payload_and_temp_path_validation(tmp_path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    begin = transfer_begin_write("dest.bin", expected_bytes=1)

    with pytest.raises(ValueError, match="not valid base64"):
        transfer_ops.transfer_write_chunk(
            "dest.bin", begin.transfer_id, 0, "%%%", context=_context()
        )
    with pytest.raises(ValueError, match="offset must be >= 0"):
        transfer_ops.transfer_write_bytes(
            "dest.bin", begin.transfer_id, -1, b"x", context=_context()
        )
    transfer_abort_write("dest.bin", begin.transfer_id)

    scratch = transfer_alloc_temp_path("unsafe/suffix")
    assert scratch.path.endswith(".bin")
    scratch_path = Path(scratch.path)
    scratch_path.mkdir()
    with pytest.raises(IsADirectoryError):
        transfer_ops.transfer_delete_temp_path(scratch.path, context=_context())
    scratch_path.rmdir()

    (root / "file.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="compression"):
        transfer_pack_dir(".", compression="zip")
    with pytest.raises(NotADirectoryError):
        transfer_pack_dir("file.txt", compression="none")


def test_finish_conflict_does_not_publish_false_commit_receipt(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    transfer_id = "conflict-no-receipt"
    begin = transfer_begin_write(
        "dest.bin",
        overwrite=False,
        expected_bytes=4,
        transfer_id=transfer_id,
    )
    transfer_ops.transfer_write_bytes(
        "dest.bin", begin.transfer_id, 0, b"data", context=_context()
    )
    (root / "dest.bin").write_bytes(b"data")

    with pytest.raises(FileExistsError):
        transfer_finish_write(
            "dest.bin",
            begin.transfer_id,
            expected_bytes=4,
            expected_sha256=hashlib.sha256(b"data").hexdigest(),
        )

    receipt_path = transfer_ops._write_receipt_path(_context(), transfer_id)
    receipt = _context().store.state_store.read_json(receipt_path)
    assert receipt is not None
    assert receipt["status"] == "receiving"
    assert "final_bytes" not in receipt
    assert "final_sha256" not in receipt


def test_file_write_binding_comes_from_executor_session_and_survives_cwd_change(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    old = root / "old"
    new = root / "new"
    old.mkdir()
    new.mkdir()
    session_id = "sess_0000000000000000000001"
    context.store.create_session(session_id=session_id, workdir=old)

    # Model an executor-authoritative cwd commit whose response was lost: the
    # caller may still project /old, but the executor session already owns /new.
    context.store.change_session_workdir(session_id, new)
    transfer_id = "executor-authoritative-binding"
    transfer_begin_write(
        "result.bin",
        overwrite=True,
        expected_bytes=4,
        transfer_id=transfer_id,
        session_id=session_id,
    )
    receipt_path = transfer_ops._write_receipt_path(context, transfer_id)
    receipt = context.store.state_store.read_json(receipt_path)
    assert receipt is not None
    assert receipt["destination"] == str(new / "result.bin")

    # Later session cwd changes must not retarget an already-bound transfer.
    context.store.change_session_workdir(session_id, old)
    transfer_ops.transfer_write_bytes(
        "result.bin",
        transfer_id,
        0,
        b"data",
        session_id=session_id,
        context=context,
    )
    transfer_finish_write(
        "result.bin",
        transfer_id,
        expected_bytes=4,
        expected_sha256=hashlib.sha256(b"data").hexdigest(),
        session_id=session_id,
    )

    assert (new / "result.bin").read_bytes() == b"data"
    assert not (old / "result.bin").exists()


def test_directory_unpack_retry_uses_executor_receipt_binding_after_cwd_change(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    source = root / "source-dir"
    source.mkdir()
    (source / "note.txt").write_text("hello", encoding="utf-8")
    old = root / "old"
    new = root / "new"
    old.mkdir()
    new.mkdir()
    session_id = "sess_0000000000000000000002"
    context.store.create_session(session_id=session_id, workdir=old)
    packed = transfer_pack_dir("source-dir", compression="gz")
    transfer_id = "directory-authoritative-binding"

    first = transfer_unpack_archive(
        packed.archive_path,
        "copied",
        transfer_id=transfer_id,
        session_id=session_id,
    )
    assert first.completed is True
    assert (old / "copied" / "note.txt").read_text(encoding="utf-8") == "hello"

    context.store.change_session_workdir(session_id, new)
    resumed = transfer_unpack_archive(
        packed.archive_path,
        "copied",
        transfer_id=transfer_id,
        session_id=session_id,
    )

    assert resumed.completed is True
    assert resumed.resumed is True
    assert (old / "copied" / "note.txt").read_text(encoding="utf-8") == "hello"
    assert not (new / "copied").exists()


def test_consumed_directory_archive_write_remains_recoverable(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    source = root / "source-dir"
    source.mkdir()
    (source / "note.txt").write_text("hello", encoding="utf-8")
    packed = transfer_pack_dir("source-dir", compression="gz")
    packed_path = transfer_ops._resolve_temp_path(
        packed.archive_path, context=context
    )
    payload = packed_path.read_bytes()
    scratch = transfer_alloc_temp_path(".tar.gz")
    transfer_id = "consumed-directory-archive"

    transfer_begin_write(
        scratch.path,
        overwrite=True,
        expected_bytes=len(payload),
        transfer_id=transfer_id,
    )
    transfer_ops.transfer_write_bytes(
        scratch.path,
        transfer_id,
        0,
        payload,
        hashlib.sha256(payload).hexdigest(),
        context=context,
    )
    transfer_finish_write(
        scratch.path,
        transfer_id,
        expected_bytes=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    unpacked = transfer_unpack_archive(
        scratch.path,
        "destination",
        cleanup_archive=True,
        transfer_id=transfer_id,
    )
    assert unpacked.completed is True
    scratch_path = transfer_ops._resolve_temp_path(
        scratch.path, context=context
    )
    assert not scratch_path.exists()

    # A control restart can retry the import after unpack committed but before
    # receipt release.  The consumed scratch archive must still be recoverable
    # from the linked completed unpack receipt.
    resumed = transfer_begin_write(
        scratch.path,
        overwrite=True,
        expected_bytes=len(payload),
        transfer_id=transfer_id,
    )
    assert resumed.completed is True
    assert resumed.sha256 == hashlib.sha256(payload).hexdigest()

    abandoned = transfer_ops.transfer_abandon_import(
        transfer_id, "dir", scratch.path, context=context
    )
    assert abandoned["safe_to_forget"] is True
    assert (root / "destination" / "note.txt").read_text(
        encoding="utf-8"
    ) == "hello"


def test_committing_receipt_with_live_temp_resumes_instead_of_guessing_commit(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    transfer_id = "committing-live-temp"
    (root / "dest.bin").write_bytes(b"old!")
    begin = transfer_begin_write(
        "dest.bin",
        overwrite=True,
        expected_bytes=4,
        transfer_id=transfer_id,
    )
    transfer_ops.transfer_write_bytes(
        "dest.bin", begin.transfer_id, 0, b"data", context=_context()
    )
    receipt = transfer_ops._write_receipt_path(_context(), transfer_id)
    transfer_ops._write_write_receipt(
        _context(),
        receipt,
        {
            "destination": str(root / "dest.bin"),
            "overwrite": True,
            "expected_bytes": 4,
            "destination_existed": True,
            "status": "committing",
            "final_bytes": 4,
            "final_sha256": hashlib.sha256(b"data").hexdigest(),
            "updated_at": time.time(),
        },
    )

    resumed = transfer_begin_write(
        "dest.bin",
        overwrite=True,
        expected_bytes=4,
        transfer_id=transfer_id,
    )

    assert resumed.completed is False
    assert resumed.resumed is True
    assert resumed.offset == 4
    assert (root / "dest.bin").read_bytes() == b"old!"


def test_unpack_prepared_receipt_without_staging_fails_closed(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    source = root / "source-dir"
    source.mkdir()
    (source / "note.txt").write_text("hello", encoding="utf-8")
    packed = transfer_pack_dir("source-dir", compression="gz")
    transfer_id = "prepared-missing-staging"
    real_write_receipt = transfer_ops._write_unpack_receipt

    def stop_after_prepared(context, path, receipt):
        real_write_receipt(context, path, receipt)
        if receipt.get("status") == "prepared":
            raise OSError("simulated crash after prepared receipt")

    monkeypatch.setattr(
        transfer_ops, "_write_unpack_receipt", stop_after_prepared
    )
    with pytest.raises(OSError, match="prepared receipt"):
        transfer_unpack_archive(
            packed.archive_path,
            "dest-dir",
            transfer_id=transfer_id,
        )

    staging = root / f".dest-dir.unpack-{transfer_id}"
    assert staging.is_dir()
    shutil.rmtree(staging)
    monkeypatch.setattr(
        transfer_ops, "_write_unpack_receipt", real_write_receipt
    )
    with pytest.raises(RuntimeError, match="prepared state lost"):
        transfer_unpack_archive(
            packed.archive_path,
            "dest-dir",
            transfer_id=transfer_id,
        )


def test_abandon_unpack_after_extraction_before_prepared_receipt(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    source = root / "source-dir"
    source.mkdir()
    (source / "note.txt").write_text("hello", encoding="utf-8")
    packed = transfer_pack_dir("source-dir", compression="gz")
    transfer_id = "extracting-crash-abandon"
    context = _context()
    receipt_path = transfer_ops._unpack_receipt_path(context, transfer_id)
    staging = root / f".dest-dir.unpack-{transfer_id}"
    archive = transfer_ops._resolve_transfer_path(
        packed.archive_path, must_exist=True, context=context
    )
    real_write_receipt = transfer_ops._write_unpack_receipt

    def crash_before_prepared(context, path, receipt):
        if receipt.get("status") == "prepared":
            raise OSError("simulated crash before prepared receipt")
        real_write_receipt(context, path, receipt)

    monkeypatch.setattr(
        transfer_ops, "_write_unpack_receipt", crash_before_prepared
    )
    with pytest.raises(OSError, match="before prepared receipt"):
        transfer_unpack_archive(
            packed.archive_path,
            "dest-dir",
            transfer_id=transfer_id,
        )

    persisted = context.store.state_store.read_json(receipt_path)
    assert persisted is not None
    assert persisted["status"] == "extracting"
    assert staging.is_dir()
    assert (staging / "note.txt").read_text(encoding="utf-8") == "hello"
    assert archive.exists()

    monkeypatch.setattr(
        transfer_ops, "_write_unpack_receipt", real_write_receipt
    )
    result = transfer_ops.transfer_abandon_import(
        transfer_id, "dir", packed.archive_path, context=context
    )

    assert result["safe_to_forget"] is True
    assert result["unpack_reconciled"] is True
    assert not staging.exists()
    assert not archive.exists()
    assert not receipt_path.exists()
    assert not (root / "dest-dir").exists()


def test_transfer_receipts_are_durable_and_explicitly_released(
    tmp_path, monkeypatch
):
    _workspace(tmp_path, monkeypatch)
    transfer_id = "durable-release"
    begin = transfer_begin_write(
        "dest.bin", expected_bytes=4, transfer_id=transfer_id
    )
    transfer_ops.transfer_write_bytes(
        "dest.bin", begin.transfer_id, 0, b"data", context=_context()
    )
    transfer_finish_write(
        "dest.bin",
        begin.transfer_id,
        expected_bytes=4,
        expected_sha256=hashlib.sha256(b"data").hexdigest(),
    )

    receipt = transfer_ops._write_receipt_path(_context(), transfer_id)
    assert (
        receipt.parent
        == _context().store.state_store.layout.executor_transfers_dir
    )
    assert receipt.exists()
    released = transfer_ops.transfer_release_receipts(
        transfer_id, context=_context()
    )
    assert released["write_receipt_deleted"] is True
    assert not receipt.exists()


def test_corrupt_durable_write_receipt_fails_closed_without_echoing_values(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    transfer_id = "corrupt-durable-receipt"
    receipt_path = transfer_ops._write_receipt_path(_context(), transfer_id)
    secret_marker = "should-never-reach-errors"
    _context().store.state_store.write_json(
        receipt_path,
        {
            "destination": str(root / "dest.bin"),
            "overwrite": True,
            "expected_bytes": 4,
            "destination_existed": False,
            "status": "completed",
            "final_bytes": 4,
            "final_sha256": hashlib.sha256(b"data").hexdigest(),
            "updated_at": time.time(),
            "unexpected": secret_marker,
        },
    )

    with pytest.raises(ValueError) as captured:
        transfer_begin_write(
            "dest.bin",
            overwrite=True,
            expected_bytes=4,
            transfer_id=transfer_id,
        )

    assert "transfer write receipt is invalid" in str(captured.value)
    assert secret_marker not in str(captured.value)
    assert not (root / "dest.bin").exists()


def test_durable_receipt_identity_validation_fails_closed(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "receipt-validation"
    destination = root / "dest.bin"
    archive = root / "archive.tar.gz"
    archive.write_bytes(b"archive")
    destination.write_bytes(b"data")

    write_path = transfer_ops._write_receipt_path(context, transfer_id)
    transfer_ops._write_write_receipt(
        context,
        write_path,
        {
            "destination": str(destination),
            "overwrite": True,
            "expected_bytes": 4,
            "destination_existed": False,
            "status": "completed",
            "final_bytes": 4,
            "final_sha256": hashlib.sha256(b"data").hexdigest(),
            "updated_at": time.time(),
            "committed_at": time.time(),
        },
    )
    resumed = transfer_begin_write(
        "other.bin",
        overwrite=True,
        expected_bytes=4,
        transfer_id=transfer_id,
    )
    assert resumed.completed is True
    assert resumed.path == "dest.bin"
    with pytest.raises(ValueError, match="overwrite mode mismatch"):
        transfer_begin_write(
            "dest.bin",
            overwrite=False,
            expected_bytes=4,
            transfer_id=transfer_id,
        )
    with pytest.raises(ValueError, match="expected size mismatch"):
        transfer_begin_write(
            "dest.bin",
            overwrite=True,
            expected_bytes=5,
            transfer_id=transfer_id,
        )

    unpack_path = transfer_ops._unpack_receipt_path(context, transfer_id)
    transfer_ops._write_unpack_receipt(
        context,
        unpack_path,
        {
            "destination": str(root / "dest-dir"),
            "archive": str(archive),
            "archive_display": "archive.tar.gz",
            "overwrite": True,
            "cleanup_archive": True,
            "entries": 1,
            "destination_existed": False,
            "status": "completed",
            "created_at": time.time(),
            "committed_at": time.time(),
        },
    )
    with pytest.raises(ValueError, match="destination mismatch"):
        transfer_ops._read_unpack_receipt(
            context,
            unpack_path,
            root / "other-dir",
            archive,
            overwrite=True,
            cleanup_archive=True,
        )
    with pytest.raises(ValueError, match="archive mismatch"):
        transfer_ops._read_unpack_receipt(
            context,
            unpack_path,
            root / "dest-dir",
            root / "other.tar.gz",
            overwrite=True,
            cleanup_archive=True,
        )
    with pytest.raises(ValueError, match="overwrite mode mismatch"):
        transfer_ops._read_unpack_receipt(
            context,
            unpack_path,
            root / "dest-dir",
            archive,
            overwrite=False,
            cleanup_archive=True,
        )
    with pytest.raises(ValueError, match="cleanup mode mismatch"):
        transfer_ops._read_unpack_receipt(
            context,
            unpack_path,
            root / "dest-dir",
            archive,
            overwrite=True,
            cleanup_archive=False,
        )
    with pytest.raises(ValueError, match="unsupported characters"):
        transfer_ops._write_receipt_path(context, "bad/id")


def test_unpack_receipt_recovers_replace_after_publish_ack_loss(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    source = root / "source-replace"
    source.mkdir()
    (source / "new.txt").write_text("new", encoding="utf-8")
    destination = root / "dest-replace"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    packed = transfer_pack_dir("source-replace", compression="gz")
    transfer_id = "replace-publish-loss"
    real_write_receipt = transfer_ops._write_unpack_receipt

    def lose_completed_receipt(context, path, receipt):
        if receipt.get("status") == "completed":
            raise OSError("simulated completed receipt loss")
        real_write_receipt(context, path, receipt)

    monkeypatch.setattr(
        transfer_ops, "_write_unpack_receipt", lose_completed_receipt
    )
    with pytest.raises(OSError, match="completed receipt loss"):
        transfer_unpack_archive(
            packed.archive_path,
            "dest-replace",
            overwrite=True,
            cleanup_archive=True,
            transfer_id=transfer_id,
        )

    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    backup = root / f".dest-replace.backup-{transfer_id}"
    assert backup.is_dir()
    monkeypatch.setattr(
        transfer_ops, "_write_unpack_receipt", real_write_receipt
    )
    recovered = transfer_unpack_archive(
        packed.archive_path,
        "dest-replace",
        overwrite=True,
        cleanup_archive=True,
        transfer_id=transfer_id,
    )
    assert recovered.resumed is True
    assert recovered.completed is True
    assert recovered.backup_deleted is True
    assert not backup.exists()


def _write_unpack_state(
    root: Path,
    *,
    transfer_id: str,
    status: str,
    destination_existed: bool,
) -> tuple[Path, Path, Path, Path]:
    context = _context()
    destination = root / "state-dest"
    archive = root / "state-archive.tar.gz"
    staging = root / f".state-dest.unpack-{transfer_id}"
    backup = root / f".state-dest.backup-{transfer_id}"
    receipt_path = transfer_ops._unpack_receipt_path(context, transfer_id)
    transfer_ops._write_unpack_receipt(
        context,
        receipt_path,
        {
            "destination": str(destination),
            "archive": str(archive),
            "archive_display": "state-archive.tar.gz",
            "overwrite": True,
            "cleanup_archive": True,
            "entries": 1,
            "destination_existed": destination_existed,
            "status": status,
            "created_at": time.time(),
        },
    )
    return destination, archive, staging, backup


@pytest.mark.parametrize(
    (
        "status",
        "destination_existed",
        "make_staging",
        "make_destination",
        "make_backup",
        "message",
    ),
    [
        ("prepared", True, True, False, False, "lost the original destination"),
        (
            "prepared",
            False,
            True,
            True,
            False,
            "destination changed during recovery",
        ),
        (
            "backup_moved",
            False,
            True,
            False,
            True,
            "backup state is inconsistent",
        ),
        ("backup_moved", True, False, False, True, "lost staging or backup"),
        (
            "backup_moved",
            True,
            True,
            True,
            True,
            "unexpectedly has a destination",
        ),
        (
            "publishing",
            True,
            True,
            False,
            False,
            "publishing state is inconsistent",
        ),
        (
            "publishing",
            False,
            True,
            True,
            False,
            "destination changed during publish",
        ),
        ("publishing", False, False, False, False, "could not confirm commit"),
        ("publishing", True, False, True, False, "lost its backup"),
    ],
)
def test_unpack_receipt_inconsistent_durable_states_fail_closed(
    tmp_path,
    monkeypatch,
    status,
    destination_existed,
    make_staging,
    make_destination,
    make_backup,
    message,
):
    root = _workspace(tmp_path, monkeypatch)
    transfer_id = "state-check"
    destination, archive, staging, backup = _write_unpack_state(
        root,
        transfer_id=transfer_id,
        status=status,
        destination_existed=destination_existed,
    )
    if make_staging:
        staging.mkdir()
    if make_destination:
        destination.mkdir()
    if make_backup:
        backup.mkdir()

    with pytest.raises(RuntimeError, match=message):
        transfer_unpack_archive(
            str(archive),
            "state-dest",
            overwrite=True,
            cleanup_archive=True,
            transfer_id=transfer_id,
        )


def test_completed_receipt_keeps_commit_proof_without_independent_ttl(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    transfer_id = "old-completed-proof"
    begin = transfer_begin_write(
        "dest.bin",
        overwrite=False,
        expected_bytes=4,
        transfer_id=transfer_id,
    )
    transfer_ops.transfer_write_bytes(
        "dest.bin", begin.transfer_id, 0, b"data", context=_context()
    )
    transfer_finish_write(
        "dest.bin",
        begin.transfer_id,
        expected_bytes=4,
        expected_sha256=hashlib.sha256(b"data").hexdigest(),
    )
    receipt_path = transfer_ops._write_receipt_path(_context(), transfer_id)
    receipt = _context().store.state_store.read_json(receipt_path)
    assert isinstance(receipt, dict)
    receipt["committed_at"] = time.time() - 365 * 24 * 60 * 60
    _context().store.state_store.write_json(receipt_path, receipt)

    unrelated = transfer_begin_write(
        "other.bin", expected_bytes=0, transfer_id="unrelated-transfer"
    )
    transfer_abort_write("other.bin", unrelated.transfer_id)

    assert receipt_path.exists()
    recovered = transfer_begin_write(
        "dest.bin",
        overwrite=False,
        expected_bytes=4,
        transfer_id=transfer_id,
    )
    assert recovered.completed is True
    assert recovered.resumed is True
    assert recovered.sha256 == hashlib.sha256(b"data").hexdigest()
    assert (root / "dest.bin").read_bytes() == b"data"


def test_abandon_import_rolls_back_prepared_directory_after_backup_move(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "abandon-prepared-dir"
    destination = root / "dest-abandon"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    staging = root / f".dest-abandon.unpack-{transfer_id}"
    staging.mkdir()
    (staging / "new.txt").write_text("new", encoding="utf-8")
    backup = root / f".dest-abandon.backup-{transfer_id}"
    archive = root / "abandon-archive.tar.gz"
    archive.write_bytes(b"archive")
    os.replace(destination, backup)
    receipt_path = transfer_ops._unpack_receipt_path(context, transfer_id)
    transfer_ops._write_unpack_receipt(
        context,
        receipt_path,
        {
            "destination": str(destination),
            "archive": str(archive),
            "archive_display": "abandon-archive.tar.gz",
            "overwrite": True,
            "cleanup_archive": True,
            "entries": 1,
            "destination_existed": True,
            "status": "prepared",
            "created_at": time.time(),
        },
    )

    result = transfer_ops.transfer_abandon_import(
        transfer_id, "dir", context=context
    )

    assert result["unpack_reconciled"] is True
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert not staging.exists()
    assert not backup.exists()
    assert not archive.exists()
    assert not receipt_path.exists()


def test_abandon_import_discards_uncommitted_final_file_write(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "abandon-committing-file"
    destination = root / "dest.bin"
    destination.write_bytes(b"old!")
    begin = transfer_begin_write(
        "dest.bin",
        overwrite=True,
        expected_bytes=4,
        transfer_id=transfer_id,
    )
    transfer_ops.transfer_write_bytes(
        "dest.bin", begin.transfer_id, 0, b"data", context=context
    )
    receipt_path = transfer_ops._write_receipt_path(context, transfer_id)
    transfer_ops._write_write_receipt(
        context,
        receipt_path,
        {
            "destination": str(destination),
            "overwrite": True,
            "expected_bytes": 4,
            "destination_existed": True,
            "status": "committing",
            "final_bytes": 4,
            "final_sha256": hashlib.sha256(b"data").hexdigest(),
            "updated_at": time.time(),
        },
    )

    result = transfer_ops.transfer_abandon_import(
        transfer_id, "file", context=context
    )

    assert result["write_reconciled"] is True
    assert destination.read_bytes() == b"old!"
    assert not receipt_path.exists()
    temporary = transfer_ops._transfer_temp_path(destination, transfer_id)
    assert not temporary.exists()
    assert not transfer_ops._transfer_metadata_path(temporary).exists()


def test_abandon_import_confirms_completed_final_file_write(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "abandon-completed-file"
    begin = transfer_begin_write(
        "committed.bin",
        overwrite=False,
        expected_bytes=4,
        transfer_id=transfer_id,
    )
    transfer_ops.transfer_write_bytes(
        "committed.bin", begin.transfer_id, 0, b"data", context=context
    )
    transfer_finish_write(
        "committed.bin",
        begin.transfer_id,
        expected_bytes=4,
        expected_sha256=hashlib.sha256(b"data").hexdigest(),
    )
    receipt_path = transfer_ops._write_receipt_path(context, transfer_id)

    result = transfer_ops.transfer_abandon_import(
        transfer_id, "file", context=context
    )

    assert result["write_reconciled"] is True
    assert (root / "committed.bin").read_bytes() == b"data"
    assert not receipt_path.exists()


@pytest.mark.parametrize(
    ("status", "publish_committed"),
    [
        ("backup_moved", False),
        ("publishing", False),
        ("publishing", True),
        ("completed", True),
    ],
)
def test_abandon_import_reconciles_directory_durable_states(
    tmp_path, monkeypatch, status, publish_committed
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = (
        f"abandon-{status}-{'commit' if publish_committed else 'rollback'}"
    )
    destination, archive, staging, backup = _write_unpack_state(
        root,
        transfer_id=transfer_id,
        status=status,
        destination_existed=True,
    )
    archive.write_bytes(b"archive")
    backup.mkdir()
    (backup / "old.txt").write_text("old", encoding="utf-8")
    if publish_committed:
        destination.mkdir()
        (destination / "new.txt").write_text("new", encoding="utf-8")
        if status == "completed":
            staging.mkdir()
            (staging / "leftover.txt").write_text("leftover", encoding="utf-8")
    else:
        staging.mkdir()
        (staging / "new.txt").write_text("new", encoding="utf-8")

    result = transfer_ops.transfer_abandon_import(
        transfer_id, "dir", context=context
    )

    assert result["unpack_reconciled"] is True
    if publish_committed:
        assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
        assert not (destination / "old.txt").exists()
    else:
        assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
        assert not (destination / "new.txt").exists()
    assert not staging.exists()
    assert not backup.exists()
    assert not archive.exists()
    assert not transfer_ops._unpack_receipt_path(context, transfer_id).exists()


def test_abandon_directory_rejects_invalid_linked_write_receipt(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "abandon-invalid-linked-receipt"
    destination, archive, staging, backup = _write_unpack_state(
        root,
        transfer_id=transfer_id,
        status="prepared",
        destination_existed=False,
    )
    archive.write_bytes(b"archive")
    staging.mkdir()
    (staging / "new.txt").write_text("new", encoding="utf-8")
    write_receipt_path = transfer_ops._write_receipt_path(context, transfer_id)
    context.store.state_store.write_json(
        write_receipt_path,
        {"destination": str(archive), "status": "completed"},
    )

    with pytest.raises(
        ValueError, match="linked transfer abandonment receipt is invalid"
    ):
        transfer_ops.transfer_abandon_import(
            transfer_id, "dir", context=context
        )

    assert archive.exists()
    assert staging.exists()
    assert not destination.exists()
    assert not backup.exists()
    assert write_receipt_path.exists()
    assert transfer_ops._unpack_receipt_path(context, transfer_id).exists()


def test_abandon_directory_rejects_mismatched_linked_receipts(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "abandon-mismatched-receipts"
    destination, archive, staging, backup = _write_unpack_state(
        root,
        transfer_id=transfer_id,
        status="prepared",
        destination_existed=False,
    )
    archive.write_bytes(b"archive")
    staging.mkdir()
    (staging / "new.txt").write_text("new", encoding="utf-8")
    write_receipt_path = transfer_ops._write_receipt_path(context, transfer_id)
    transfer_ops._write_write_receipt(
        context,
        write_receipt_path,
        {
            "destination": str(root / "different-archive.tar.gz"),
            "overwrite": True,
            "expected_bytes": 7,
            "destination_existed": False,
            "status": "completed",
            "final_bytes": 7,
            "final_sha256": hashlib.sha256(b"archive").hexdigest(),
            "updated_at": time.time(),
            "committed_at": time.time(),
        },
    )

    with pytest.raises(
        ValueError, match="transfer archive write receipt identity mismatch"
    ):
        transfer_ops.transfer_abandon_import(
            transfer_id, "dir", context=context
        )

    assert archive.exists()
    assert staging.exists()
    assert not destination.exists()
    assert not backup.exists()
    assert write_receipt_path.exists()
    assert transfer_ops._unpack_receipt_path(context, transfer_id).exists()


def test_abandon_directory_retry_after_backup_restore_crash(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "abandon-retry-restore"
    destination, archive, staging, backup = _write_unpack_state(
        root,
        transfer_id=transfer_id,
        status="backup_moved",
        destination_existed=True,
    )
    archive.write_bytes(b"archive")
    staging.mkdir()
    (staging / "new.txt").write_text("new", encoding="utf-8")
    backup.mkdir()
    (backup / "old.txt").write_text("old", encoding="utf-8")
    receipt_path = transfer_ops._unpack_receipt_path(context, transfer_id)
    real_replace = os.replace
    crashed = False

    def crash_after_restore(source, target):
        nonlocal crashed
        real_replace(source, target)
        if (
            Path(source) == backup
            and Path(target) == destination
            and not crashed
        ):
            crashed = True
            raise OSError("simulated crash after restore")

    monkeypatch.setattr(transfer_ops.os, "replace", crash_after_restore)
    with pytest.raises(OSError, match="simulated crash after restore"):
        transfer_ops.transfer_abandon_import(
            transfer_id, "dir", context=context
        )

    persisted = context.store.state_store.read_json(receipt_path)
    assert persisted is not None
    assert persisted["abandonment_status"] == "rollback"
    assert destination.exists()
    assert not backup.exists()
    assert staging.exists()

    monkeypatch.setattr(transfer_ops.os, "replace", real_replace)
    result = transfer_ops.transfer_abandon_import(
        transfer_id, "dir", context=context
    )

    assert result["unpack_reconciled"] is True
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert not staging.exists()
    assert not archive.exists()
    assert not receipt_path.exists()


def test_abandon_directory_retry_after_backup_delete_crash(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "abandon-retry-delete-backup"
    destination, archive, staging, backup = _write_unpack_state(
        root,
        transfer_id=transfer_id,
        status="publishing",
        destination_existed=True,
    )
    archive.write_bytes(b"archive")
    destination.mkdir()
    (destination / "new.txt").write_text("new", encoding="utf-8")
    backup.mkdir()
    (backup / "old.txt").write_text("old", encoding="utf-8")
    receipt_path = transfer_ops._unpack_receipt_path(context, transfer_id)
    real_remove = transfer_ops._remove_existing_path
    crashed = False

    def crash_after_backup_delete(path):
        nonlocal crashed
        real_remove(path)
        if Path(path) == backup and not crashed:
            crashed = True
            raise OSError("simulated crash after backup delete")

    monkeypatch.setattr(
        transfer_ops, "_remove_existing_path", crash_after_backup_delete
    )
    with pytest.raises(OSError, match="simulated crash after backup delete"):
        transfer_ops.transfer_abandon_import(
            transfer_id, "dir", context=context
        )

    persisted = context.store.state_store.read_json(receipt_path)
    assert persisted is not None
    assert persisted["abandonment_status"] == "commit"
    assert destination.exists()
    assert not backup.exists()

    monkeypatch.setattr(transfer_ops, "_remove_existing_path", real_remove)
    result = transfer_ops.transfer_abandon_import(
        transfer_id, "dir", context=context
    )

    assert result["unpack_reconciled"] is True
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert not archive.exists()
    assert not receipt_path.exists()


def test_abandon_import_discards_prepared_new_directory(tmp_path, monkeypatch):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "abandon-prepared-new-dir"
    destination, archive, staging, backup = _write_unpack_state(
        root,
        transfer_id=transfer_id,
        status="prepared",
        destination_existed=False,
    )
    archive.write_bytes(b"archive")
    staging.mkdir()
    (staging / "new.txt").write_text("new", encoding="utf-8")

    result = transfer_ops.transfer_abandon_import(
        transfer_id, "dir", context=context
    )

    assert result["unpack_reconciled"] is True
    assert not destination.exists()
    assert not staging.exists()
    assert not backup.exists()
    assert not archive.exists()


def test_abandon_import_without_receipts_is_idempotent(tmp_path, monkeypatch):
    _workspace(tmp_path, monkeypatch)

    result = transfer_ops.transfer_abandon_import(
        "already-clean", "file", context=_context()
    )

    assert result == {
        "safe_to_forget": True,
        "write_reconciled": False,
        "unpack_reconciled": False,
    }


def test_abandon_directory_before_unpack_removes_committed_scratch_archive(
    tmp_path, monkeypatch
):
    _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "abandon-dir-before-unpack"
    scratch = transfer_alloc_temp_path(".tar.gz")
    begin = transfer_begin_write(
        scratch.path,
        overwrite=True,
        expected_bytes=7,
        transfer_id=transfer_id,
    )
    transfer_ops.transfer_write_bytes(
        scratch.path, begin.transfer_id, 0, b"archive", context=context
    )
    transfer_finish_write(
        scratch.path,
        begin.transfer_id,
        expected_bytes=7,
        expected_sha256=hashlib.sha256(b"archive").hexdigest(),
    )
    archive_path = transfer_ops._resolve_temp_path(
        scratch.path, context=context
    )
    assert archive_path.exists()

    result = transfer_ops.transfer_abandon_import(
        transfer_id, "dir", context=context
    )

    assert result == {
        "safe_to_forget": True,
        "write_reconciled": True,
        "unpack_reconciled": False,
    }
    assert not archive_path.exists()
    assert not transfer_ops._write_receipt_path(context, transfer_id).exists()


def test_abandon_scratch_archive_retry_after_unlink_crash(
    tmp_path, monkeypatch
):
    _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "abandon-dir-unlink-crash"
    scratch = transfer_alloc_temp_path(".tar.gz")
    begin = transfer_begin_write(
        scratch.path,
        overwrite=True,
        expected_bytes=7,
        transfer_id=transfer_id,
    )
    transfer_ops.transfer_write_bytes(
        scratch.path, begin.transfer_id, 0, b"archive", context=context
    )
    transfer_finish_write(
        scratch.path,
        begin.transfer_id,
        expected_bytes=7,
        expected_sha256=hashlib.sha256(b"archive").hexdigest(),
    )
    archive_path = transfer_ops._resolve_temp_path(
        scratch.path, context=context
    )
    receipt_path = transfer_ops._write_receipt_path(context, transfer_id)
    real_unlink = Path.unlink
    crashed = False

    def crash_after_archive_unlink(path, *args, **kwargs):
        nonlocal crashed
        result = real_unlink(path, *args, **kwargs)
        if path == archive_path and not crashed:
            crashed = True
            raise OSError("simulated crash after archive unlink")
        return result

    monkeypatch.setattr(Path, "unlink", crash_after_archive_unlink)
    with pytest.raises(OSError, match="simulated crash after archive unlink"):
        transfer_ops.transfer_abandon_import(
            transfer_id, "dir", context=context
        )

    persisted = context.store.state_store.read_json(receipt_path)
    assert persisted is not None
    assert persisted["abandonment_status"] == "discard_destination"
    assert not archive_path.exists()
    assert receipt_path.exists()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    result = transfer_ops.transfer_abandon_import(
        transfer_id, "dir", context=context
    )

    assert result["safe_to_forget"] is True
    assert result["write_reconciled"] is True
    assert not archive_path.exists()
    assert not receipt_path.exists()

    # If the executor dies after removing the receipt but before returning the
    # RPC result, the same durable transfer identity must still be confirmable.
    repeated = transfer_ops.transfer_abandon_import(
        transfer_id, "dir", scratch.path, context=context
    )
    assert repeated == {
        "safe_to_forget": True,
        "write_reconciled": False,
        "unpack_reconciled": False,
    }


def test_abandon_receiving_write_retries_persisted_discard_temp(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "abandon-retry-discard-temp"
    transfer_begin_write("dest.bin", expected_bytes=8, transfer_id=transfer_id)
    transfer_ops.transfer_write_bytes(
        "dest.bin", transfer_id, 0, b"part", context=context
    )
    destination = root / "dest.bin"
    temporary = transfer_ops._transfer_temp_path(destination, transfer_id)
    metadata_path = transfer_ops._transfer_metadata_path(temporary)
    receipt_path = transfer_ops._write_receipt_path(context, transfer_id)
    receipt = context.store.state_store.read_json(receipt_path)
    assert isinstance(receipt, dict)
    receipt["abandonment_status"] = "discard_temp"
    context.store.state_store.write_json(receipt_path, receipt)

    result = transfer_ops.transfer_abandon_import(
        transfer_id, "file", context=context
    )

    assert result["safe_to_forget"] is True
    assert result["write_reconciled"] is True
    assert not destination.exists()
    assert not temporary.exists()
    assert not metadata_path.exists()
    assert not receipt_path.exists()


def test_abandon_archive_retries_persisted_discard_destination(
    tmp_path, monkeypatch
):
    _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "abandon-retry-discard-destination"
    scratch = transfer_alloc_temp_path(".tar.gz")
    transfer_begin_write(
        scratch.path,
        expected_bytes=7,
        transfer_id=transfer_id,
    )
    transfer_ops.transfer_write_bytes(
        scratch.path, transfer_id, 0, b"archive", context=context
    )
    transfer_finish_write(
        scratch.path,
        transfer_id,
        expected_bytes=7,
        expected_sha256=hashlib.sha256(b"archive").hexdigest(),
    )
    archive_path = transfer_ops._resolve_temp_path(
        scratch.path, context=context
    )
    receipt_path = transfer_ops._write_receipt_path(context, transfer_id)
    receipt = context.store.state_store.read_json(receipt_path)
    assert isinstance(receipt, dict)
    receipt["abandonment_status"] = "discard_destination"
    context.store.state_store.write_json(receipt_path, receipt)

    result = transfer_ops.transfer_abandon_import(
        transfer_id, "dir", context=context
    )

    assert result["safe_to_forget"] is True
    assert result["write_reconciled"] is True
    assert not archive_path.exists()
    assert not receipt_path.exists()


def test_abandon_import_cleans_receiving_write_from_executor_binding(
    tmp_path, monkeypatch
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "abandon-receiving-file"
    session_workdir = root / "session-workdir"
    session_workdir.mkdir()
    begin = transfer_begin_write(
        "receiving.bin",
        overwrite=True,
        expected_bytes=8,
        transfer_id=transfer_id,
        workdir="session-workdir",
    )
    transfer_ops.transfer_write_bytes(
        "receiving.bin",
        begin.transfer_id,
        0,
        b"part",
        context=context,
    )
    destination = session_workdir / "receiving.bin"
    temporary = transfer_ops._transfer_temp_path(destination, transfer_id)
    metadata_path = transfer_ops._transfer_metadata_path(temporary)
    assert temporary.exists()
    assert metadata_path.exists()
    receipt_path = transfer_ops._write_receipt_path(context, transfer_id)
    receipt = context.store.state_store.read_json(receipt_path)
    assert receipt is not None
    assert receipt["status"] == "receiving"
    assert receipt["destination"] == str(destination)

    result = transfer_ops.transfer_abandon_import(
        transfer_id,
        "file",
        "receiving.bin",
        context=context,
    )

    assert result == {
        "safe_to_forget": True,
        "write_reconciled": True,
        "unpack_reconciled": False,
    }
    assert not temporary.exists()
    assert not metadata_path.exists()
    assert not receipt_path.exists()
    assert not destination.exists()


def test_abandon_directory_receipt_allows_restricted_internal_scratch_archive(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_root))
    root = _workspace(workspace, monkeypatch)
    context = _context()
    transfer_id = "abandon-restricted-dir"
    scratch = transfer_alloc_temp_path(".tar.gz")
    archive = transfer_ops._resolve_temp_path(scratch.path, context=context)
    assert root not in archive.parents
    archive.write_bytes(b"archive")

    destination = root / "restricted-dest"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    staging = root / f".restricted-dest.unpack-{transfer_id}"
    staging.mkdir()
    (staging / "new.txt").write_text("new", encoding="utf-8")
    backup = root / f".restricted-dest.backup-{transfer_id}"
    os.replace(destination, backup)
    receipt_path = transfer_ops._unpack_receipt_path(context, transfer_id)
    transfer_ops._write_unpack_receipt(
        context,
        receipt_path,
        {
            "destination": str(destination),
            "archive": str(archive),
            "archive_display": scratch.path,
            "overwrite": True,
            "cleanup_archive": True,
            "entries": 1,
            "destination_existed": True,
            "status": "prepared",
            "created_at": time.time(),
        },
    )

    result = transfer_ops.transfer_abandon_import(
        transfer_id, "dir", scratch.path, context=context
    )

    assert result["safe_to_forget"] is True
    assert result["unpack_reconciled"] is True
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert not staging.exists()
    assert not backup.exists()
    assert not archive.exists()
    assert not receipt_path.exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("size", "size changed"),
        ("digest", "digest changed"),
        ("temporary", "unexpectedly retains"),
    ],
)
def test_abandon_completed_file_fails_closed_when_commit_proof_changes(
    tmp_path, monkeypatch, mutation, message
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = f"abandon-file-{mutation}"
    begin = transfer_begin_write(
        "committed.bin",
        overwrite=True,
        expected_bytes=4,
        transfer_id=transfer_id,
    )
    transfer_ops.transfer_write_bytes(
        "committed.bin", begin.transfer_id, 0, b"data", context=context
    )
    transfer_finish_write(
        "committed.bin",
        begin.transfer_id,
        expected_bytes=4,
        expected_sha256=hashlib.sha256(b"data").hexdigest(),
    )
    destination = root / "committed.bin"
    if mutation == "size":
        destination.write_bytes(b"longer")
    elif mutation == "digest":
        destination.write_bytes(b"nope")
    else:
        transfer_ops._transfer_temp_path(destination, transfer_id).write_bytes(
            b"temp"
        )
    receipt_path = transfer_ops._write_receipt_path(context, transfer_id)

    with pytest.raises(RuntimeError, match=message):
        transfer_ops.transfer_abandon_import(
            transfer_id, "file", context=context
        )

    assert receipt_path.exists()


def test_abandon_invalid_write_receipt_fails_closed(tmp_path, monkeypatch):
    _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "abandon-invalid-write"
    receipt_path = transfer_ops._write_receipt_path(context, transfer_id)
    context.store.state_store.write_json(receipt_path, {"status": "completed"})

    with pytest.raises(ValueError, match="write receipt is invalid"):
        transfer_ops.transfer_abandon_import(
            transfer_id, "file", context=context
        )

    assert receipt_path.exists()


@pytest.mark.parametrize(
    (
        "status",
        "destination_existed",
        "make_staging",
        "make_destination",
        "make_backup",
        "message",
    ),
    [
        ("prepared", True, True, True, True, "cannot be safely abandoned"),
        ("prepared", False, True, False, True, "unexpected backup"),
        (
            "backup_moved",
            False,
            True,
            False,
            True,
            "cannot be safely abandoned",
        ),
        ("publishing", True, True, False, False, "cannot be rolled back"),
        ("publishing", False, True, False, True, "unexpected backup"),
        ("publishing", False, False, False, False, "cannot confirm commit"),
        ("publishing", True, False, True, False, "lost its backup"),
        ("publishing", False, False, True, True, "unexpected backup"),
        ("completed", True, False, False, False, "directory is missing"),
    ],
)
def test_abandon_directory_inconsistent_states_fail_closed(
    tmp_path,
    monkeypatch,
    status,
    destination_existed,
    make_staging,
    make_destination,
    make_backup,
    message,
):
    root = _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "abandon-inconsistent"
    destination, archive, staging, backup = _write_unpack_state(
        root,
        transfer_id=transfer_id,
        status=status,
        destination_existed=destination_existed,
    )
    archive.write_bytes(b"archive")
    if make_staging:
        staging.mkdir()
    if make_destination:
        destination.mkdir()
    if make_backup:
        backup.mkdir()
    receipt_path = transfer_ops._unpack_receipt_path(context, transfer_id)

    with pytest.raises(RuntimeError, match=message):
        transfer_ops.transfer_abandon_import(
            transfer_id, "dir", context=context
        )

    assert receipt_path.exists()


def test_abandon_invalid_unpack_receipt_fails_closed(tmp_path, monkeypatch):
    _workspace(tmp_path, monkeypatch)
    context = _context()
    transfer_id = "abandon-invalid-unpack"
    receipt_path = transfer_ops._unpack_receipt_path(context, transfer_id)
    context.store.state_store.write_json(receipt_path, {"status": "prepared"})

    with pytest.raises(ValueError, match="unpack receipt is invalid"):
        transfer_ops.transfer_abandon_import(
            transfer_id, "dir", context=context
        )

    assert receipt_path.exists()
