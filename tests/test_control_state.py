from pathlib import Path

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
        requested_workdir="~/src/workgate",
        resolved_workdir_display="/home/user/src/workgate",
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
    assert read_limits == [None, None]


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

    with pytest.raises(RuntimeError, match="Invalid control session registry"):
        state.start()

    assert state.snapshot_executors() == {}
    assert state.snapshot_sessions() == {}
