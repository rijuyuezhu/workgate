from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from workgate.control.state import (
    ControlSessionRecord,
    ControlState,
    ExecutorTrustRecord,
)
from workgate.persistence import FileStateStore
from workgate.protocol.credentials import (
    executor_credential_verifier,
    new_executor_credential,
)
from workgate.protocol.ids import new_executor_id, new_session_id


def _store(tmp_path: Path) -> FileStateStore:
    return FileStateStore(lambda: tmp_path / "state")


def _registry_json(store: FileStateStore, path: Path) -> dict[str, Any]:
    payload = store.read_json(path)
    assert isinstance(payload, dict)
    return payload


def _trust_record(*, executor_id: str | None = None) -> ExecutorTrustRecord:
    credential = new_executor_credential()
    return ExecutorTrustRecord(
        executor_id=executor_id or new_executor_id(),
        name="laptop",
        credential_verifier=executor_credential_verifier(credential),
        created_at=10,
    )


def _session_record(
    executor_id: str, *, session_id: str | None = None
) -> ControlSessionRecord:
    return ControlSessionRecord(
        session_id=session_id or new_session_id(),
        executor_id=executor_id,
        workdir="/home/user/src/workgate",
        label="workgate",
        status="creating",
        created_at=20,
        updated_at=20,
    )


def test_control_state_restores_trust_and_session_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    first = ControlState(store)
    first.start()
    trust = _trust_record()
    session = _session_record(trust.executor_id)

    first.put_executor(trust)
    first.put_session(session)
    first.close()

    assert first.snapshot_executors() == {}
    assert first.snapshot_sessions() == {}

    read_limits: list[int | None] = []
    original_read_json = store.read_json

    def observe_read(path: Path, *, max_bytes: int | None = None):
        read_limits.append(max_bytes)
        return original_read_json(path, max_bytes=max_bytes)

    monkeypatch.setattr(store, "read_json", observe_read)
    restored = ControlState(store)
    restored.start()

    assert restored.snapshot_executors() == {trust.executor_id: trust}
    assert restored.snapshot_sessions() == {session.session_id: session}
    assert read_limits and set(read_limits) == {None}


def test_executor_trust_persists_only_verifier_and_explicit_revocation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    credential = new_executor_credential()
    record = ExecutorTrustRecord(
        executor_id=new_executor_id(),
        name="executor",
        credential_verifier=executor_credential_verifier(credential),
        created_at=1,
    )
    state = ControlState(store)
    state.start()
    state.put_executor(record)

    payload = store.read_json(store.layout.control_executors_path)
    assert credential not in repr(payload)
    assert "last_seen_at" not in repr(payload)

    revoked = state.revoke_executor(record.executor_id, revoked_at=99)
    assert revoked.revoked_at == 99

    restored = ControlState(store)
    restored.start()
    assert restored.snapshot_executors()[record.executor_id].revoked_at == 99


def test_control_session_record_rejects_legacy_routing_identity() -> None:
    executor_id = new_executor_id()
    payload = _session_record(executor_id).model_dump()

    for legacy_field, value in (
        ("target", "remote"),
        ("machine", "laptop"),
        ("worker_session_id", "legacy"),
    ):
        with pytest.raises(ValidationError):
            ControlSessionRecord.model_validate(
                {**payload, legacy_field: value}
            )


def test_control_state_rejects_session_rebinding(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = ControlState(store)
    state.start()
    original = _session_record(new_executor_id())
    state.put_session(original)
    rebound = original.model_copy(update={"executor_id": new_executor_id()})

    with pytest.raises(ValueError, match="binding cannot change"):
        state.put_session(rebound)

    assert state.snapshot_sessions() == {original.session_id: original}


def test_control_state_write_failure_does_not_publish_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    state = ControlState(store)
    state.start()
    record = _trust_record()

    def fail_write(_path: Path, _value: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "write_json", fail_write)

    with pytest.raises(OSError, match="disk full"):
        state.put_executor(record)

    assert state.snapshot_executors() == {}


def test_control_state_rejects_corrupt_registry_without_partial_publish(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    valid = _trust_record()
    store.write_json(
        store.layout.control_executors_path,
        {
            "version": 1,
            "executors": [valid.model_dump(mode="json")],
        },
    )
    store.write_json(
        store.layout.control_sessions_path,
        {"version": 1, "sessions": [{"session_id": "bad"}]},
    )
    state = ControlState(store)

    with pytest.raises(RuntimeError, match="Unrecoverable control registry"):
        state.start()

    assert state.snapshot_executors() == {}
    assert state.snapshot_sessions() == {}


@pytest.mark.parametrize("registry", ["executors", "sessions"])
def test_control_registry_recovers_corrupt_primary_from_matching_generation(
    tmp_path: Path, registry: str
) -> None:
    store = _store(tmp_path)
    state = ControlState(store)
    state.start()
    trust = _trust_record()
    state.put_executor(trust)
    session = _session_record(trust.executor_id)
    state.put_session(session)
    path = (
        store.layout.control_executors_path
        if registry == "executors"
        else store.layout.control_sessions_path
    )
    primary = _registry_json(store, path)
    assert isinstance(primary["generation"], str)
    assert primary == _registry_json(store, path.with_name(path.name + ".bak"))
    assert _registry_json(store, path.with_name(path.name + ".generation")) == {
        "generation": primary["generation"]
    }
    path.write_text("corrupt{", encoding="utf-8")

    restarted = ControlState(store)
    restarted.start()
    assert restarted.snapshot_executors() == {trust.executor_id: trust}
    assert restarted.snapshot_sessions() == {session.session_id: session}
    assert _registry_json(store, path) == primary


@pytest.mark.parametrize("registry", ["executors", "sessions"])
def test_control_registry_rejects_stale_backup_after_primary_corruption(
    tmp_path: Path, registry: str
) -> None:
    store = _store(tmp_path)
    state = ControlState(store)
    state.start()
    trust = _trust_record()
    state.put_executor(trust)
    session = _session_record(trust.executor_id)
    state.put_session(session)
    path = (
        store.layout.control_executors_path
        if registry == "executors"
        else store.layout.control_sessions_path
    )
    previous = _registry_json(store, path)
    if registry == "executors":
        state.revoke_executor(trust.executor_id, revoked_at=100)
    else:
        state.update_session(session.session_id, status="ended", updated_at=50)
    current = _registry_json(store, path)
    assert previous["generation"] != current["generation"]
    store.write_json(path.with_name(path.name + ".bak"), previous)
    path.write_text("{invalid", encoding="utf-8")
    restarted = ControlState(store)
    with pytest.raises(RuntimeError, match="Unrecoverable control registry"):
        restarted.start()
    assert restarted.snapshot_executors() == {}
    assert restarted.snapshot_sessions() == {}


@pytest.mark.parametrize("registry", ["executors", "sessions"])
def test_control_registry_healthy_primary_repairs_stale_recovery_files(
    tmp_path: Path, registry: str
) -> None:
    store = _store(tmp_path)
    state = ControlState(store)
    state.start()
    trust = _trust_record()
    state.put_executor(trust)
    session = _session_record(trust.executor_id)
    state.put_session(session)
    path = (
        store.layout.control_executors_path
        if registry == "executors"
        else store.layout.control_sessions_path
    )
    original = _registry_json(store, path)
    if registry == "executors":
        state.revoke_executor(trust.executor_id, revoked_at=100)
    else:
        state.update_session(session.session_id, status="ended", updated_at=50)
    current = _registry_json(store, path)
    store.write_json(path.with_name(path.name + ".bak"), original)
    store.write_json(
        path.with_name(path.name + ".generation"),
        {"generation": original["generation"]},
    )
    restored = ControlState(store)
    restored.start()
    assert _registry_json(store, path) == current
    assert _registry_json(store, path.with_name(path.name + ".bak")) == current
    assert _registry_json(store, path.with_name(path.name + ".generation")) == {
        "generation": current["generation"]
    }
    if registry == "executors":
        assert (
            restored.snapshot_executors()[trust.executor_id].revoked_at == 100
        )
    else:
        assert (
            restored.snapshot_sessions()[session.session_id].status == "ended"
        )


def test_control_registry_interrupted_commit_keeps_memory_and_repairs_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    state = ControlState(store)
    state.start()
    trust = _trust_record()
    state.put_executor(trust)
    path = store.layout.control_executors_path
    original = _registry_json(store, path)
    write_json = store.write_json

    def fail_primary(target: Path, value: object) -> None:
        if target == path:
            raise OSError("simulated primary write failure")
        write_json(target, value)

    monkeypatch.setattr(store, "write_json", fail_primary)
    with pytest.raises(OSError, match="simulated primary"):
        state.revoke_executor(trust.executor_id, revoked_at=100)
    assert state.snapshot_executors()[trust.executor_id].revoked_at is None
    assert _registry_json(store, path) == original
    # Marker is now newer than backup, but healthy primary still wins and
    # recovers the exact old trusted generation rather than a speculative one.
    monkeypatch.setattr(store, "write_json", write_json)
    restarted = ControlState(store)
    restarted.start()
    assert restarted.snapshot_executors()[trust.executor_id].revoked_at is None
    assert _registry_json(store, path.with_name(path.name + ".bak")) == original
    assert _registry_json(store, path.with_name(path.name + ".generation")) == {
        "generation": original["generation"]
    }


def test_control_registry_backup_write_failure_preserves_committed_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    state = ControlState(store)
    state.start()
    trust = _trust_record()
    state.put_executor(trust)
    path = store.layout.control_executors_path
    backup = path.with_name(path.name + ".bak")
    write_json = store.write_json

    def fail_backup(target: Path, value: object) -> None:
        if target == backup:
            raise OSError("simulated backup write failure")
        write_json(target, value)

    monkeypatch.setattr(store, "write_json", fail_backup)
    state.revoke_executor(trust.executor_id, revoked_at=100)
    assert state.snapshot_executors()[trust.executor_id].revoked_at == 100
    assert (
        _registry_json(store, backup)["generation"]
        != _registry_json(store, path)["generation"]
    )
    monkeypatch.setattr(store, "write_json", write_json)
    restarted = ControlState(store)
    restarted.start()
    assert restarted.snapshot_executors()[trust.executor_id].revoked_at == 100
    assert _registry_json(store, backup) == _registry_json(store, path)


def test_control_registry_invalid_primary_generation_does_not_bypass_fence(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    state = ControlState(store)
    state.start()
    trust = _trust_record()
    state.put_executor(trust)
    path = store.layout.control_executors_path
    payload = _registry_json(store, path)
    payload["generation"] = "malformed-generation"
    store.write_json(path, payload)
    restored = ControlState(store)
    restored.start()
    assert restored.snapshot_executors() == {trust.executor_id: trust}
    assert (
        _registry_json(store, path)["generation"]
        == _registry_json(store, path.with_name(path.name + ".generation"))[
            "generation"
        ]
    )


def test_control_registry_never_resets_when_primary_and_backup_unusable(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    state = ControlState(store)
    state.start()
    trust = _trust_record()
    state.put_executor(trust)
    path = store.layout.control_executors_path
    path.write_text("invalid primary", encoding="utf-8")
    path.with_name(path.name + ".bak").write_text(
        "invalid backup", encoding="utf-8"
    )
    restored = ControlState(store)
    with pytest.raises(RuntimeError, match="Unrecoverable control registry"):
        restored.start()
    assert restored.snapshot_executors() == {}


def test_control_registry_upgrades_valid_legacy_primary_with_fenced_backup(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    trust = _trust_record()
    path = store.layout.control_executors_path
    store.write_json(
        path,
        {"version": 1, "executors": [trust.model_dump(mode="json")]},
    )
    state = ControlState(store)
    state.start()
    assert state.snapshot_executors() == {trust.executor_id: trust}
    current = _registry_json(store, path)
    assert len(current["generation"]) == 32
    assert _registry_json(store, path.with_name(path.name + ".bak")) == current


def test_control_registry_rejects_missing_primary_with_stale_fence(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    state = ControlState(store)
    state.start()
    trust = _trust_record()
    state.put_executor(trust)
    path = store.layout.control_executors_path
    backup = path.with_name(path.name + ".bak")
    store.write_json(
        backup, {**_registry_json(store, backup), "generation": "0" * 32}
    )
    path.unlink()
    restored = ControlState(store)
    with pytest.raises(RuntimeError, match="Unrecoverable control registry"):
        restored.start()
    assert restored.snapshot_executors() == {}


def test_control_session_task_binding_survives_primary_recovery(
    tmp_path: Path,
) -> None:
    from workgate.protocol.ids import new_task_id

    store = _store(tmp_path)
    state = ControlState(store)
    state.start()
    trust = _trust_record()
    state.put_executor(trust)
    session = _session_record(trust.executor_id)
    state.put_session(session)
    task_id = new_task_id()
    bound = state.attach_session_task(session.session_id, task_id)
    path = store.layout.control_sessions_path
    path.write_text("truncated", encoding="utf-8")
    restored = ControlState(store)
    restored.start()
    assert restored.snapshot_sessions()[session.session_id] == bound
    assert restored.snapshot_sessions()[session.session_id].task_id == task_id


def test_control_session_task_binding_cannot_rollback_through_stale_backup(
    tmp_path: Path,
) -> None:
    from workgate.protocol.ids import new_task_id

    store = _store(tmp_path)
    state = ControlState(store)
    state.start()
    trust = _trust_record()
    state.put_executor(trust)
    session = _session_record(trust.executor_id)
    state.put_session(session)
    path = store.layout.control_sessions_path
    unbound = _registry_json(store, path)
    state.attach_session_task(session.session_id, new_task_id())
    store.write_json(path.with_name(path.name + ".bak"), unbound)
    path.write_text("damaged", encoding="utf-8")
    restored = ControlState(store)
    with pytest.raises(RuntimeError, match="Unrecoverable control registry"):
        restored.start()
    assert restored.snapshot_sessions() == {}


def test_control_registry_healthy_primary_loads_when_backup_repair_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    state = ControlState(store)
    state.start()
    trust = _trust_record()
    state.put_executor(trust)
    path = store.layout.control_executors_path
    path.with_name(path.name + ".bak").unlink()
    original_write = store.write_json

    def fail_backup(target: Path, value: object) -> None:
        if target == path.with_name(path.name + ".bak"):
            raise OSError("cannot repair mirror")
        original_write(target, value)

    monkeypatch.setattr(store, "write_json", fail_backup)
    recovered = ControlState(store)
    recovered.start()
    assert recovered.snapshot_executors()[trust.executor_id] == trust


def test_control_registry_dangling_primary_symlink_is_not_first_run(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    path = store.layout.control_executors_path
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.symlink_to(path.parent / "missing.json")
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(RuntimeError, match="Unrecoverable control registry"):
        ControlState(store).start()
