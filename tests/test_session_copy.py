from __future__ import annotations

from typing import Any

import pytest

import workgate.control.session_copy as session_copy_module
from tests.helpers import build_paired_control_harness
from workgate.config.settings import clear_settings_cache, get_settings
from workgate.control.session_copy import (
    SESSION_COPY_MANAGED_KIND,
    ControlSessionCopyService,
)
from workgate.schemas.result_models.jobs import JobStartOutput


def _configure(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WORKGATE_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKGATE_STATE_DIR", str(tmp_path / ".state"))
    monkeypatch.setenv("WORKGATE_AGENT_BRIDGE_ENABLED", "false")
    clear_settings_cache()


async def _paired_sessions(tmp_path, monkeypatch):
    _configure(monkeypatch, tmp_path)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dst").mkdir(parents=True, exist_ok=True)
    harness = build_paired_control_harness(get_settings())
    src = await harness.control.session_coordinator.start_session(
        workdir="src", executor_id=harness.executor_id
    )
    dst = await harness.control.session_coordinator.start_session(
        workdir="dst", executor_id=harness.executor_id
    )
    assert isinstance(src, dict)
    assert isinstance(dst, dict)
    return harness, str(src["session_id"]), str(dst["session_id"])


@pytest.mark.asyncio
async def test_shared_session_copy_streams_file_on_same_executor(
    tmp_path, monkeypatch
):
    payload = b"abcdef" * 1000
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "payload.bin").write_bytes(payload)
    harness, src_id, dst_id = await _paired_sessions(tmp_path, monkeypatch)
    progress: list[dict[str, Any]] = []

    async def report(value: dict[str, Any]) -> None:
        progress.append(value)

    result = await harness.control.session_copy_service.copy(
        src_session_id=src_id,
        src_path="payload.bin",
        dst_session_id=dst_id,
        dst_path="copied.bin",
        chunk_size=128,
        progress=report,
    )

    assert result.kind == "file"
    assert result.relation.route == "same_executor"
    assert result.relation.same_executor is True
    assert result.relation.same_session is False
    assert result.transport == "same_executor"
    assert result.bytes == len(payload)
    assert result.chunks > 1
    assert result.source.session_id == src_id
    assert result.destination.session_id == dst_id
    assert (tmp_path / "dst" / "copied.bin").read_bytes() == payload
    assert progress[0]["phase"] == "stat"
    assert progress[-1]["bytes_transferred"] == len(payload)


@pytest.mark.asyncio
async def test_shared_session_copy_packs_and_unpacks_directory(
    tmp_path, monkeypatch
):
    (tmp_path / "src" / "tree" / "nested").mkdir(parents=True)
    (tmp_path / "src" / "tree" / "nested" / "note.txt").write_text(
        "hello", encoding="utf-8"
    )
    (tmp_path / "src" / "tree" / "data.bin").write_bytes(b"\x00\x01")
    harness, src_id, dst_id = await _paired_sessions(tmp_path, monkeypatch)

    result = await harness.control.session_copy_service.copy(
        src_session_id=src_id,
        src_path="tree",
        dst_session_id=dst_id,
        dst_path="tree-copy",
        kind="dir",
        chunk_size=256,
    )

    assert result.kind == "dir"
    assert result.archive_bytes is not None and result.archive_bytes > 0
    assert result.entries is not None and result.entries >= 2
    assert result.cleanup_errors == []
    assert (tmp_path / "dst" / "tree-copy" / "nested" / "note.txt").read_text(
        encoding="utf-8"
    ) == "hello"
    assert (
        tmp_path / "dst" / "tree-copy" / "data.bin"
    ).read_bytes() == b"\x00\x01"


@pytest.mark.asyncio
async def test_shared_session_copy_rejects_kind_mismatch(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "payload.txt").write_text("file", encoding="utf-8")
    harness, src_id, dst_id = await _paired_sessions(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="does not match source type"):
        await harness.control.session_copy_service.copy(
            src_session_id=src_id,
            src_path="payload.txt",
            dst_session_id=dst_id,
            dst_path="payload-copy",
            kind="dir",
        )


@pytest.mark.asyncio
async def test_shared_session_copy_preserves_destination_when_overwrite_is_false(
    tmp_path, monkeypatch
):
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dst").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "payload.txt").write_text("new", encoding="utf-8")
    (tmp_path / "dst" / "payload.txt").write_text("old", encoding="utf-8")
    harness, src_id, dst_id = await _paired_sessions(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="transfer_begin_write failed"):
        await harness.control.session_copy_service.copy(
            src_session_id=src_id,
            src_path="payload.txt",
            dst_session_id=dst_id,
            dst_path="payload.txt",
            overwrite=False,
        )

    assert (tmp_path / "dst" / "payload.txt").read_text(
        encoding="utf-8"
    ) == "old"


@pytest.mark.asyncio
async def test_managed_copy_rejects_changed_session_binding(
    tmp_path, monkeypatch
):
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "payload.txt").write_text("data", encoding="utf-8")
    harness, src_id, dst_id = await _paired_sessions(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="source session binding changed"):
        await harness.control.session_copy_service.copy(
            src_session_id=src_id,
            src_path="payload.txt",
            dst_session_id=dst_id,
            dst_path="payload.txt",
            expected_bindings={
                src_id: {
                    "executor_id": harness.executor_id,
                    "workdir": "/stale",
                }
            },
        )


@pytest.mark.asyncio
async def test_background_copy_snapshots_bindings_before_launch(
    tmp_path, monkeypatch
):
    harness, src_id, dst_id = await _paired_sessions(tmp_path, monkeypatch)
    captured: dict[str, Any] = {}

    async def fake_start(
        session_id: str,
        managed_kind: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> JobStartOutput:
        captured.update(
            session_id=session_id,
            managed_kind=managed_kind,
            payload=payload,
            kwargs=kwargs,
        )
        return JobStartOutput(
            job_id="job_copy",
            kind="managed",
            name="copy-result.bin",
            status="running",
            command="session_copy",
            cwd=".",
            session_id=src_id,
            created_at=1.0,
            updated_at=1.0,
            attempts=1,
        )

    monkeypatch.setattr(
        session_copy_module,
        "start_managed_job_without_session_admission",
        fake_start,
    )

    started = await harness.control.session_copy_service.start_background(
        src_session_id=src_id,
        src_path="payload.bin",
        dst_session_id=dst_id,
        dst_path="result.bin",
        chunk_size=1234,
    )

    assert started.job_id == "job_copy"
    assert captured["session_id"] == src_id
    assert captured["managed_kind"] == SESSION_COPY_MANAGED_KIND
    payload = captured["payload"]
    assert payload["src_session_id"] == src_id
    assert payload["dst_session_id"] == dst_id
    assert payload["bindings"][src_id]["executor_id"] == harness.executor_id
    assert payload["bindings"][dst_id]["executor_id"] == harness.executor_id
    assert payload["chunk_size"] >= 1234


def test_session_copy_managed_registration_is_runtime_bound() -> None:
    service = object.__new__(ControlSessionCopyService)
    kind, handler = service.managed_job_registration()

    assert kind == SESSION_COPY_MANAGED_KIND
    assert handler == service._run_managed_job
