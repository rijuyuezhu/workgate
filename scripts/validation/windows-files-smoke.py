"""Native Windows filesystem smoke for WorkGate; Python stdlib only.

Run from an actual Windows host with Python 3.14:
  python scripts/validation/windows-files-smoke.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

if sys.platform != "win32":
    raise SystemExit("Windows-native smoke requires Windows CPython")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workgate.app_paths import ensure_private_directory, resolve_app_paths
from workgate.executor.path import prune_temp_dir, resolve_path
from workgate.platform.path import path_lock_key, remote_path_name
from workgate.platform.windows.archive import (
    extract_archive,
    inspect_archive,
    move_directory,
    pack_directory,
    remove_directory_tree,
)
from workgate.platform.windows.locks import try_lock_handle, unlock_handle
from workgate.platform.windows.nt_relative import (
    close_handle,
    open_child,
    pinned_parent,
    rename_child,
)
from workgate.platform.windows.security import (
    atomic_write_workspace_bytes,
    copy_workspace_file,
    create_workspace_file_no_follow,
    delete_open_workspace_file,
    delete_private_file,
    delete_workspace_file,
    open_regular_file_no_follow,
    replace_workspace_file,
)
from workgate.utils.private_files import (
    append_private_bytes,
    atomic_write_private_bytes,
    private_file_lock,
    write_private_bytes,
)


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--check-held-lock":
        with Path(sys.argv[2]).open("r+b") as handle:
            acquired = try_lock_handle(handle)
            if acquired:
                unlock_handle(handle)
        return 1 if acquired else 0
    if len(sys.argv) == 3 and sys.argv[1] == "--probe-archive-write":
        try:
            with Path(sys.argv[2]).open("r+b") as handle:
                handle.write(b"mutated")
        except PermissionError:
            return 0
        return 1

    paths = resolve_app_paths()
    assert paths.config_dir.is_absolute()
    assert paths.state_dir.is_absolute()
    assert (
        str(paths.config_dir)
        .casefold()
        .startswith(os.environ["APPDATA"].casefold())
    )
    assert (
        str(paths.state_dir)
        .casefold()
        .startswith(os.environ["LOCALAPPDATA"].casefold())
    )
    assert (
        str(paths.data_dir)
        .casefold()
        .startswith(os.environ["LOCALAPPDATA"].casefold())
    )
    assert paths.runtime_dir.is_absolute()
    assert remote_path_name(r"C:\Projects\src\report.txt") == "report.txt"
    assert path_lock_key(Path(r"C:\Projects\a\File.txt")) == path_lock_key(
        Path(r"c:/projects/A/FILE.TXT")
    )

    with tempfile.TemporaryDirectory(prefix="workgate-native-files-") as tmp:
        private = ensure_private_directory(Path(tmp) / "private")
        assert ensure_private_directory(private) == private
        nested = ensure_private_directory(private / "new" / "nested")
        assert nested.is_dir()
        secret = private / "secret"
        scratch = ensure_private_directory(private / "scratch")
        write_private_bytes(scratch / "orphan.tmp", b"unclaimed")
        prune_temp_dir(
            max_files=0,
            max_bytes=0,
            directory=scratch,
            minimum_age_s=0,
        )
        assert not (scratch / "orphan.tmp").exists()
        disposable = private / "cancelled-transfer.tmp"
        write_private_bytes(disposable, b"aborted")
        delete_private_file(disposable)
        assert not disposable.exists()
        try:
            delete_private_file(disposable)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError(
                "NT private deletion did not report missing file"
            )
        write_private_bytes(secret, b"first")
        append_private_bytes(secret, b"-appended")
        assert secret.read_bytes() == b"first-appended"
        atomic_write_private_bytes(secret, b"replaced")
        assert secret.read_bytes() == b"replaced"
        path = private / "lock"
        with private_file_lock(path):
            child = subprocess.run(
                [sys.executable, __file__, "--check-held-lock", str(path)],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            assert child.returncode == 0, (
                child.returncode,
                child.stdout,
                child.stderr,
            )
        assert path.read_bytes() == b"\0"
        with private_file_lock(path):
            pass
        unsafe = Path(tmp) / "unsafe"
        unsafe.write_bytes(b"unprotected")
        try:
            write_private_bytes(unsafe, b"must-not-write")
        except PermissionError:
            assert unsafe.read_bytes() == b"unprotected"
        else:
            raise AssertionError("Insecure existing file was modified")
        try:
            atomic_write_private_bytes(unsafe, b"must-not-publish")
        except PermissionError:
            assert unsafe.read_bytes() == b"unprotected"
        else:
            raise AssertionError(
                "Atomic publish replaced insecure existing file"
            )
        assert list(Path(tmp).glob(".unsafe.*.tmp")) == []
        staging = private / "transfer-staging"
        write_private_bytes(staging, b"from-staging")
        workspace_file = Path(tmp) / "workspace-destination"
        workspace_file.write_bytes(b"existing")
        replace_workspace_file(staging, workspace_file)
        assert not staging.exists()
        assert workspace_file.read_bytes() == b"from-staging"
        upload = Path(tmp) / "user-upload.bin"
        with os.fdopen(create_workspace_file_no_follow(upload), "wb") as writer:
            writer.write(b"uploaded")
            writer.flush()
            os.fsync(writer.fileno())
        assert upload.read_bytes() == b"uploaded"
        try:
            create_workspace_file_no_follow(upload)
        except FileExistsError:
            pass
        else:
            raise AssertionError(
                "NT exclusive workspace file creation overwrote data"
            )
        fail_upload = Path(tmp) / "partial-upload.bin"
        with os.fdopen(
            create_workspace_file_no_follow(fail_upload), "wb"
        ) as partial:
            partial.write(b"partial")
            delete_open_workspace_file(partial.fileno())
        assert not fail_upload.exists()
        atomic_target = Path(tmp) / "write-atomic.txt"
        assert atomic_write_workspace_bytes(
            atomic_target, b"initial", overwrite=False
        )
        assert atomic_target.read_bytes() == b"initial"
        try:
            atomic_write_workspace_bytes(
                atomic_target, b"forbidden", overwrite=False
            )
        except FileExistsError:
            assert atomic_target.read_bytes() == b"initial"
        else:
            raise AssertionError(
                "Windows exclusive atomic text write clobbered a file"
            )
        import hashlib

        old_sha = hashlib.sha256(b"initial").hexdigest()
        assert not atomic_write_workspace_bytes(
            atomic_target,
            b"updated",
            overwrite=True,
            expected_sha256=old_sha,
        )
        assert atomic_target.read_bytes() == b"updated"
        try:
            atomic_write_workspace_bytes(
                atomic_target,
                b"bad",
                overwrite=True,
                expected_sha256=old_sha,
            )
        except ValueError:
            assert atomic_target.read_bytes() == b"updated"
        else:
            raise AssertionError("Windows stale snapshot text write succeeded")
        assert not list(Path(tmp).glob(".write-atomic.txt.*.tmp"))
        protected = private / "protected-atomic-workspace.txt"
        write_private_bytes(protected, b"private-original")
        assert not atomic_write_workspace_bytes(
            protected, b"private-replaced", overwrite=True
        )
        from workgate.platform.windows.security import open_private_file

        with os.fdopen(open_private_file(protected, create=False), "rb") as f:
            assert f.read() == b"private-replaced"
        move_from = Path(tmp) / "ui-move-source.txt"
        move_from.write_bytes(b"native-workspace")
        move_to = Path(tmp) / "ui-move-target.txt"
        replace_workspace_file(
            move_from, move_to, private_source=False, replace=False
        )
        assert not move_from.exists()
        assert move_to.read_bytes() == b"native-workspace"
        copied_file = Path(tmp) / "ui-copy-target.txt"
        copy_workspace_file(move_to, copied_file)
        assert copied_file.read_bytes() == b"native-workspace"
        try:
            copy_workspace_file(move_to, copied_file)
        except FileExistsError:
            assert copied_file.read_bytes() == b"native-workspace"
        else:
            raise AssertionError(
                "Native file copy clobbered existing destination"
            )
        protected_copy = private / "source-to-copy.txt"
        write_private_bytes(protected_copy, b"private-copy")
        protected_result = Path(tmp) / "protected-copy.txt"
        copy_workspace_file(protected_copy, protected_result)
        from workgate.platform.windows.security import open_private_file

        with os.fdopen(
            open_private_file(protected_result, create=False), "rb"
        ) as f:
            assert f.read() == b"private-copy"
        delete_workspace_file(move_to)
        assert not move_to.exists()

        directory_to_remove = Path(tmp) / "ui-delete-directory"
        directory_to_remove.mkdir()
        (directory_to_remove / "child.txt").write_bytes(b"recursive")
        remove_directory_tree(directory_to_remove)
        assert not directory_to_remove.exists()
        public = Path(tmp) / "source.txt"
        public.write_bytes(b"public")
        with os.fdopen(open_regular_file_no_follow(public), "rb") as handle:
            assert handle.read() == b"public"
        with os.fdopen(
            open_regular_file_no_follow(public, writable=True), "r+b"
        ) as handle:
            handle.seek(0, os.SEEK_END)
            handle.write(b"-extended")
        assert public.read_bytes() == b"public-extended"
        symlink = Path(tmp) / "source-link"
        try:
            symlink.symlink_to(public)
        except OSError:
            print("WARNING: native file symlink unavailable")
        else:
            try:
                open_regular_file_no_follow(symlink)
            except PermissionError:
                pass
            else:
                raise AssertionError("Windows transfer followed a file symlink")
            symlink.unlink()
        junction = Path(tmp) / "junction"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(private)],
            capture_output=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 0:
            try:
                ensure_private_directory(junction)
            except PermissionError:
                pass
            else:
                raise AssertionError(
                    "Reparse point accepted as private directory"
                )
            try:
                write_private_bytes(junction / "must-not-appear", b"blocked")
            except PermissionError:
                assert not (private / "must-not-appear").exists()
            else:
                raise AssertionError(
                    "Windows parent junction permitted private write"
                )
            try:
                open_regular_file_no_follow(junction / "secret")
            except PermissionError:
                pass
            else:
                raise AssertionError(
                    "Windows parent junction permitted transfer read"
                )
            original_syntax = resolve_path(
                "junction/secret",
                base=Path(tmp),
                follow_final_symlink=False,
            )
            assert original_syntax.parent == junction
            try:
                open_regular_file_no_follow(original_syntax)
            except PermissionError:
                pass
            else:
                raise AssertionError(
                    "Windows resolver bypassed junction validation"
                )
            junction.rmdir()
        else:
            print("WARNING: junction test unavailable")
    with tempfile.TemporaryDirectory(prefix="workgate-archive-pack-") as temp:
        import tarfile

        root = Path(temp)
        source_dir = root / "source"
        source_dir.mkdir()
        (source_dir / "nested").mkdir()
        (source_dir / "nested" / "note.txt").write_bytes(b"native archive")
        (source_dir / "empty").mkdir()
        (source_dir / "zeros.bin").write_bytes(b"\\0" * 21)
        archive_path = root / "packed.tar.gz"
        pack_directory(
            source_dir,
            archive_path,
            compression="gz",
            max_entries=12,
            max_bytes=1024,
        )
        with tarfile.open(archive_path, "r:gz") as tar:
            assert set(tar.getnames()) == {
                "nested",
                "nested/note.txt",
                "empty",
                "zeros.bin",
            }
            assert (
                tar.extractfile("nested/note.txt").read() == b"native archive"
            )
        for entries, total_bytes in ((2, 1024), (12, 10)):
            try:
                pack_directory(
                    source_dir,
                    root / f"limited-{entries}-{total_bytes}.tar",
                    compression="none",
                    max_entries=entries,
                    max_bytes=total_bytes,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("Archive resource limit not enforced")
        with os.fdopen(
            open_regular_file_no_follow(archive_path, share_write=False), "rb"
        ):
            denied = subprocess.run(
                [
                    sys.executable,
                    __file__,
                    "--probe-archive-write",
                    str(archive_path),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            assert denied.returncode == 0, (
                denied.returncode,
                denied.stdout,
                denied.stderr,
            )
        inspected, archived_sha = inspect_archive(
            archive_path, max_entries=12, max_bytes=1024
        )
        assert inspected == 4 and len(archived_sha) == 64
        staging = root / "unpack-staging"
        member_count = extract_archive(
            archive_path,
            staging,
            max_entries=12,
            max_bytes=1024,
            expected_sha256=archived_sha,
        )
        assert member_count == 4
        assert (
            staging / "nested" / "note.txt"
        ).read_bytes() == b"native archive"
        assert (staging / "empty").is_dir()
        published = root / "published"
        move_directory(staging, published, require_private_source=True)
        assert not staging.exists()
        assert (
            published / "nested" / "note.txt"
        ).read_bytes() == b"native archive"
        insecure_source = root / "insecure-source"
        insecure_source.mkdir()
        try:
            move_directory(
                insecure_source,
                root / "unexpected-publish",
                require_private_source=True,
            )
        except PermissionError:
            assert insecure_source.is_dir()
            assert not (root / "unexpected-publish").exists()
        else:
            raise AssertionError("Insecure substitute directory was published")
        backup = root / "backup"
        move_directory(published, backup)
        move_directory(backup, published)
        remove_directory_tree(published, require_private_root=True)
        assert not published.exists()
        nonprivate_tree = root / "normal-directory"
        nonprivate_tree.mkdir()
        (nonprivate_tree / "keep.txt").write_bytes(b"do not delete")
        try:
            remove_directory_tree(nonprivate_tree, require_private_root=True)
        except PermissionError:
            assert (
                nonprivate_tree / "keep.txt"
            ).read_bytes() == b"do not delete"
        else:
            raise AssertionError(
                "Private-only NT cleanup deleted a normal directory"
            )

        for bad_name in (
            "../escaped",
            "file:ads",
            "CON",
            r"folder\..\escape",
            "NUL.txt",
        ):
            hostile_archive = root / (f"malformed-{abs(hash(bad_name))}.tar")
            with tarfile.open(hostile_archive, "w") as tar:
                entry = tarfile.TarInfo(bad_name)
                entry.size = 1
                tar.addfile(entry, __import__("io").BytesIO(b"x"))
            hostile_staging = root / "hostile-staging"
            try:
                extract_archive(
                    hostile_archive,
                    hostile_staging,
                    max_entries=12,
                    max_bytes=1024,
                )
            except ValueError:
                assert not hostile_staging.exists()
            else:
                raise AssertionError(f"unsafe tar member accepted: {bad_name}")
        tampered_archive = root / "tampered.tar"
        pack_directory(
            source_dir,
            tampered_archive,
            compression="none",
            max_entries=12,
            max_bytes=1024,
        )
        original_count, original_sha = inspect_archive(
            tampered_archive, max_entries=12, max_bytes=1024
        )
        assert original_count == 4
        with tampered_archive.open("ab") as stream:
            stream.write(b"tamper")
        tampered_stage = root / "tampered-staging"
        try:
            extract_archive(
                tampered_archive,
                tampered_stage,
                max_entries=12,
                max_bytes=1024,
                expected_sha256=original_sha,
            )
        except ValueError:
            assert not tampered_stage.exists()
        else:
            raise AssertionError(
                "Altered archive accepted during receipt replay"
            )

        collision_archive = root / "collision.tar"
        with tarfile.open(collision_archive, "w") as tar:
            from io import BytesIO

            for name in ("Report.TXT", "report.txt"):
                member = tarfile.TarInfo(name)
                member.size = 1
                tar.addfile(member, BytesIO(b"x"))
        collision_staging = root / "collision-staging"
        try:
            extract_archive(
                collision_archive,
                collision_staging,
                max_entries=12,
                max_bytes=1024,
            )
        except ValueError:
            assert not collision_staging.exists()
        else:
            raise AssertionError("Case-colliding Windows tar paths accepted")

        junction = root / "source" / "junction"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(root)],
            capture_output=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 0:
            try:
                pack_directory(
                    source_dir,
                    root / "unsafe.tar",
                    compression="none",
                    max_entries=12,
                    max_bytes=1024,
                )
            except PermissionError:
                pass
            else:
                raise AssertionError("Archive traversed a junction")
            junction.rmdir()
        else:
            raise AssertionError(
                "Windows junction creation unavailable for pack test"
            )

    with tempfile.TemporaryDirectory(prefix="workgate-parent-swap-") as temp:
        original = ensure_private_directory(Path(temp) / "original")
        write_private_bytes(original / "source", b"original-secret")
        write_private_bytes(original / "replacement", b"new-secure-secret")
        with pinned_parent(original / "source") as parent:
            moved = Path(temp) / "moved"
            original.rename(moved)
            original.mkdir()
            (original / "source").write_bytes(b"attacker-substitute")
            imported = open_child(
                parent, "source", access=0x80000000, directory=False
            )
            import msvcrt

            with os.fdopen(
                msvcrt.open_osfhandle(imported, os.O_RDONLY | os.O_BINARY), "rb"
            ) as source:
                assert source.read() == b"original-secret"
            rename_source = open_child(
                parent,
                "replacement",
                access=0x80000000 | 0x10000,
                directory=False,
            )
            from workgate.platform.windows.security import _libs

            kernel, _ = _libs()
            try:
                rename_child(rename_source, parent, "source")
            finally:
                close_handle(kernel, rename_source)
            assert (moved / "source").read_bytes() == b"new-secure-secret"
            assert (original / "source").read_bytes() == b"attacker-substitute"
    print(
        "Windows native ACLs, atomic files, locks, reparse and transfer handles: PASS"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
