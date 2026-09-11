from pathlib import Path
from typing import Any, cast

import pytest

import workgate.executor.transfer as transfer_ops
import workgate.executor.transfer_composition as transfer_composition
from workgate.config.settings import Settings
from workgate.executor.config import resolve_executor_config
from workgate.executor.runtime import build_executor_runtime
from workgate.executor.tool_session.store import ToolSessionStore


@pytest.mark.asyncio
async def test_composed_transfer_uses_explicit_executor_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    runtime_base = tmp_path / "runtime"
    runtime_base.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    source_dir = workspace / "tree"
    source_dir.mkdir(parents=True)
    (source_dir / "file.txt").write_bytes(b"payload\n")
    settings = Settings(
        workspace_root=workspace,
        state_dir=tmp_path / "state",
        agent_bridge_enabled=False,
    )
    runtime = build_executor_runtime(
        resolve_executor_config(settings), enable_control_connection=False
    )
    session_id = "sess_0000000000000000000001"
    runtime.services.tool_session_store.create_session(
        session_id=session_id,
        workdir=workspace,
    )

    settings.workspace_root = tmp_path / "wrong-workspace"
    settings.max_transfer_archive_entries = 1
    settings.max_transfer_unpacked_bytes = 1
    settings.max_tmp_files = 0
    settings.max_tmp_bytes = 1
    assert not hasattr(transfer_ops, "get_settings")
    assert not hasattr(transfer_ops, "get_tool_session_store")

    stat = await runtime.dispatcher.execute(
        "transfer_stat", {"session_id": session_id, "path": "tree/file.txt"}
    )
    assert stat.type == "file"
    assert stat.size == len(b"payload\n")

    scratch = await runtime.dispatcher.execute(
        "transfer_alloc_temp_path",
        {"session_id": session_id, "suffix": ".bin"},
    )
    assert Path(scratch.path).parent == runtime.config.temp_dir

    packed = await runtime.dispatcher.execute(
        "transfer_pack_dir",
        {"session_id": session_id, "path": "tree", "compression": "gz"},
    )
    archive = Path(packed.archive_path)
    assert archive.parent == runtime.config.temp_dir
    assert archive.exists()

    unpacked = await runtime.dispatcher.execute(
        "transfer_unpack_archive",
        {
            "session_id": session_id,
            "archive_path": packed.archive_path,
            "dst_path": "unpacked",
            "overwrite": True,
            "cleanup_archive": True,
        },
    )
    assert unpacked.completed is True
    assert (workspace / "unpacked" / "file.txt").read_text(
        encoding="utf-8"
    ) == ("payload\n")
    assert not archive.exists()


@pytest.mark.asyncio
async def test_transfer_composition_preserves_unbound_command_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = build_executor_runtime(
        resolve_executor_config(
            Settings(
                workspace_root=workspace,
                state_dir=tmp_path / "state",
                agent_bridge_enabled=False,
            )
        ),
        enable_control_connection=False,
    )

    class Store:
        def __init__(self) -> None:
            self.admitted: list[str] = []

        def admit_active_session(self, session_id: str) -> None:
            self.admitted.append(session_id)

    store = Store()
    handlers = transfer_composition.build_transfer_handlers(
        runtime.config, cast(ToolSessionStore, store)
    )
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def fake_to_thread(func: Any, *args: Any, **kwargs: Any) -> str:
        calls.append((func.__name__, args, kwargs))
        return func.__name__

    monkeypatch.setattr(
        transfer_composition.asyncio, "to_thread", fake_to_thread
    )
    common = {"session_id": "sess-command", "workdir": str(workspace)}

    assert (
        await handlers["transfer_read_chunk"](
            {**common, "path": "source.bin", "offset": 4, "chunk_size": 16}
        )
        == "transfer_read_chunk"
    )
    assert (
        await handlers["transfer_begin_write"](
            {**common, "path": "dst.bin", "expected_bytes": 8}
        )
        == "transfer_begin_write"
    )
    assert (
        await handlers["transfer_write_chunk"](
            {
                **common,
                "path": "dst.bin",
                "transfer_id": "transfer-1",
                "offset": 0,
                "data_b64": "eA==",
            }
        )
        == "transfer_write_chunk"
    )
    assert (
        await handlers["transfer_finish_write"](
            {**common, "path": "dst.bin", "transfer_id": "transfer-1"}
        )
        == "transfer_finish_write"
    )
    assert (
        await handlers["transfer_abort_write"](
            {**common, "path": "dst.bin", "transfer_id": "transfer-1"}
        )
        == "transfer_abort_write"
    )
    assert (
        await handlers["transfer_copy_file"](
            {
                "session_id": "sess-command",
                "source_path": "source.bin",
                "destination_path": "dst.bin",
                "source_workdir": str(workspace),
                "destination_workdir": str(workspace),
            }
        )
        == "transfer_copy_file"
    )
    assert (
        await handlers["transfer_delete_temp_path"](
            {
                "session_id": "sess-command",
                "path": str(tmp_path / "scratch.bin"),
            }
        )
        == "transfer_delete_temp_path"
    )

    assert store.admitted == ["sess-command"] * 7
    assert [name for name, _args, _kwargs in calls] == [
        "transfer_read_chunk",
        "transfer_begin_write",
        "transfer_write_chunk",
        "transfer_finish_write",
        "transfer_abort_write",
        "transfer_copy_file",
        "transfer_delete_temp_path",
    ]
    assert (
        transfer_composition._transfer_session_id(
            {"session_id": "sess-command", "_workgate_unbound_temp": True}
        )
        is None
    )
