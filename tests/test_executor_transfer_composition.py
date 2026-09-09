from pathlib import Path

import pytest

import workgate.executor.transfer as transfer_ops
from workgate.config.settings import Settings
from workgate.executor.runtime import build_executor_runtime


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
    (source_dir / "file.txt").write_text("payload\n", encoding="utf-8")
    settings = Settings(
        workspace_root=workspace,
        state_dir=tmp_path / "state",
        remote_enabled=False,
        agent_bridge_enabled=False,
    )
    runtime = build_executor_runtime(settings, enable_control_connection=False)
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
