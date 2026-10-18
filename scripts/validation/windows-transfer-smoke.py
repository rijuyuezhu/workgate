"""Real Windows transfer receipts, chunked copy and restart-resume smoke."""

import base64
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if os.name != "nt":
    raise RuntimeError("Run with native Windows Python")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from workgate.config.executor import resolve_executor_config  # noqa: E402
from workgate.config.settings import clear_settings_cache, get_settings  # noqa: E402
from workgate.executor.tool_session.store import ToolSessionStore  # noqa: E402
from workgate.executor.transfer import (  # noqa: E402
    TransferContext,
    transfer_abort_write,
    transfer_begin_write,
    transfer_finish_write,
    transfer_pack_dir,
    transfer_read_chunk,
    transfer_stat,
    transfer_unpack_archive,
    transfer_write_chunk,
)
from workgate.persistence import FileStateStore, use_state_store  # noqa: E402


def resume_in_fresh_process(
    root: Path, transfer_id: str, offset: int, size: int, sha256: str
) -> None:
    """Restore the receipt in a new Windows interpreter/process."""
    os.environ["WORKGATE_DEFAULT_WORKDIR"] = str(root)
    os.environ["WORKGATE_STATE_DIR"] = str(root / ".workgate")
    os.environ["WORKGATE_AGENT_BRIDGE_ENABLED"] = "false"
    clear_settings_cache()
    config = resolve_executor_config(get_settings())
    store = ToolSessionStore(
        state_store=FileStateStore(lambda: config.state_dir),
        default_workdir=config.default_workdir,
        max_session_snapshots=config.max_session_snapshots,
        max_session_snapshot_bytes=config.max_session_snapshot_bytes,
    )
    context = TransferContext(config, store)
    with use_state_store(FileStateStore(lambda: config.state_dir)):
        resumed = transfer_begin_write(
            "destination.txt",
            overwrite=False,
            expected_bytes=size,
            transfer_id=transfer_id,
            context=context,
        )
        assert resumed.transfer_id == transfer_id and resumed.resumed
        assert resumed.offset == offset
        remaining = (root / "source.txt").read_bytes()[offset:]
        transfer_write_chunk(
            "destination.txt",
            transfer_id,
            offset,
            base64.b64encode(remaining).decode(),
            hashlib.sha256(remaining).hexdigest(),
            context=context,
        )
        completed = transfer_finish_write(
            "destination.txt",
            transfer_id,
            expected_bytes=size,
            expected_sha256=sha256,
            context=context,
        )
        assert completed.sha256 == sha256
        print("Windows transfer receipt resumed in fresh process: PASS")


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="workgate-native-transfer-") as tmp:
        root = Path(tmp)
        os.environ["WORKGATE_DEFAULT_WORKDIR"] = str(root)
        os.environ["WORKGATE_STATE_DIR"] = str(root / ".workgate")
        os.environ["WORKGATE_AGENT_BRIDGE_ENABLED"] = "false"
        clear_settings_cache()
        config = resolve_executor_config(get_settings())

        def context() -> TransferContext:
            store = ToolSessionStore(
                state_store=FileStateStore(lambda: config.state_dir),
                default_workdir=config.default_workdir,
                max_session_snapshots=config.max_session_snapshots,
                max_session_snapshot_bytes=config.max_session_snapshot_bytes,
            )
            return TransferContext(config, store)

        with use_state_store(FileStateStore(lambda: config.state_dir)):
            source = root / "source.txt"
            source.write_bytes(b"Windows persistent chunk transfer")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            session = context()
            stat = transfer_stat("source.txt", context=session)
            assert stat.sha256 == digest and stat.size == source.stat().st_size
            read = transfer_read_chunk(
                "source.txt", offset=0, chunk_size=512, context=session
            )
            assert base64.b64decode(read.data_b64) == source.read_bytes()

            begin = transfer_begin_write(
                "destination.txt",
                overwrite=False,
                expected_bytes=stat.size,
                context=session,
            )
            split = stat.size // 2
            first = source.read_bytes()[:split]
            transfer_write_chunk(
                "destination.txt",
                begin.transfer_id,
                0,
                base64.b64encode(first).decode(),
                hashlib.sha256(first).hexdigest(),
                context=session,
            )
            # This is a different native Windows process, not just a fresh
            # TransferContext within the same interpreter.
            child = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--resume",
                    str(root),
                    begin.transfer_id,
                    str(split),
                    str(stat.size),
                    digest,
                ],
                capture_output=True,
                text=True,
                timeout=45,
                check=True,
            )
            assert "fresh process: PASS" in child.stdout, child.stderr
            assert (
                root / "destination.txt"
            ).read_bytes() == source.read_bytes()
            again = transfer_begin_write(
                "destination.txt",
                overwrite=False,
                expected_bytes=stat.size,
                transfer_id=begin.transfer_id,
                context=context(),
            )
            assert again.completed and again.resumed
            assert again.sha256 == digest

            try:
                transfer_begin_write(
                    "destination.txt",
                    overwrite=False,
                    expected_bytes=5,
                    context=context(),
                )
            except FileExistsError:
                assert (
                    root / "destination.txt"
                ).read_bytes() == source.read_bytes()
            else:
                raise AssertionError(
                    "Windows no-clobber transfer accepted existing file"
                )

            incomplete = transfer_begin_write(
                "unfinished.txt",
                overwrite=False,
                expected_bytes=5,
                context=context(),
            )
            transfer_write_chunk(
                "unfinished.txt",
                incomplete.transfer_id,
                0,
                base64.b64encode(b"abc").decode(),
                hashlib.sha256(b"abc").hexdigest(),
                context=context(),
            )
            transfer_abort_write(
                "unfinished.txt", incomplete.transfer_id, context=context()
            )
            assert not (root / "unfinished.txt").exists()

            tree = root / "directory-source"
            tree.mkdir()
            (tree / "one.txt").write_bytes(b"first")
            nested = tree / "nested"
            nested.mkdir()
            (nested / "two.bin").write_bytes(b"second")
            packed = transfer_pack_dir(
                "directory-source", compression="none", context=context()
            )
            assert packed.bytes > 0 and len(packed.sha256) == 64
            try:
                transfer_unpack_archive(
                    packed.archive_path,
                    "invalid-archive-destination",
                    overwrite=False,
                    cleanup_archive=False,
                    expected_archive_bytes=packed.bytes,
                    expected_archive_sha256="0" * 64,
                    context=context(),
                )
            except ValueError:
                assert not (root / "invalid-archive-destination").exists()
            else:
                raise AssertionError("Wrong archive checksum was accepted")
            transfer_unpack_archive(
                packed.archive_path,
                "directory-destination",
                overwrite=False,
                cleanup_archive=False,
                expected_archive_bytes=packed.bytes,
                expected_archive_sha256=packed.sha256,
                context=context(),
            )
            assert (
                root / "directory-destination/one.txt"
            ).read_bytes() == b"first"
            assert (
                root / "directory-destination/nested/two.bin"
            ).read_bytes() == b"second"
            try:
                transfer_unpack_archive(
                    packed.archive_path,
                    "directory-destination",
                    overwrite=False,
                    cleanup_archive=False,
                    expected_archive_bytes=packed.bytes,
                    expected_archive_sha256=packed.sha256,
                    context=context(),
                )
            except FileExistsError:
                assert (
                    root / "directory-destination/one.txt"
                ).read_bytes() == b"first"
            else:
                raise AssertionError(
                    "Directory transfer clobbered existing destination"
                )
    print("Windows native transfer resume, identity and no-clobber: PASS")


if __name__ == "__main__":
    if len(sys.argv) == 7 and sys.argv[1] == "--resume":
        resume_in_fresh_process(
            Path(sys.argv[2]),
            sys.argv[3],
            int(sys.argv[4]),
            int(sys.argv[5]),
            sys.argv[6],
        )
    else:
        run()
