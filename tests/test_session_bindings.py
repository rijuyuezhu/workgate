from dataclasses import replace

import pytest

from workgate.tool_session.bindings import SessionBinding, binding_from_record
from workgate.tool_session.records import AgentSession, session_to_payload


def _record() -> AgentSession:
    return AgentSession(
        session_id="sess_0000000000000000000001",
        workdir="/workspace/project",
        created_at=1.0,
        updated_at=2.0,
    )


def test_binding_from_record_returns_shared_session_binding() -> None:
    binding = binding_from_record(_record())

    assert binding == SessionBinding(
        session_id="sess_0000000000000000000001",
        workdir="/workspace/project",
    )


@pytest.mark.parametrize(
    "record, message",
    [
        (replace(_record(), session_id="invalid"), "invalid session_id"),
        (replace(_record(), workdir=""), "empty workdir"),
    ],
)
def test_binding_from_record_rejects_impossible_record_shapes(
    record: AgentSession, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        binding_from_record(record)


def test_session_to_payload_round_trips_canonical_record() -> None:
    record = _record()

    assert session_to_payload(record) == {
        "session_id": record.session_id,
        "workdir": record.workdir,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "label": None,
        "termination_requested_at": None,
        "persistent_shell_ids": [],
    }
