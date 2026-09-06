from __future__ import annotations

import ast
import base64
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from workgate.protocol import EXECUTOR_PROTOCOL_VERSION
from workgate.protocol.credentials import (
    ExecutorTrustRecord,
    executor_credential_is_trusted,
    executor_credential_verifier,
    new_executor_credential,
)
from workgate.protocol.errors import ProtocolError, ProtocolErrorCode
from workgate.protocol.executor import (
    ExecutorCommand,
    ExecutorHelloRequest,
    ExecutorHelloResponse,
    ExecutorResult,
    ExecutorRuntimeSummary,
    SessionInventorySummary,
)
from workgate.protocol.ids import (
    new_command_id,
    new_device_code,
    new_executor_id,
    new_session_id,
    new_user_code,
)
from workgate.protocol.pairing import (
    PairApprovalRequest,
    PairDecision,
    PairingExecutorMetadata,
    PairPollRequest,
    PairPollSuccess,
    PairStartRequest,
    PairStartResponse,
)


def _decoded_token_bytes(value: str, prefix: str) -> bytes:
    encoded = value.removeprefix(prefix)
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)


def test_protocol_ids_are_url_safe_and_have_required_randomness() -> None:
    executor_id = new_executor_id()
    session_id = new_session_id()
    command_id = new_command_id()
    device_code = new_device_code()

    assert len(_decoded_token_bytes(executor_id, "exec_")) == 16
    assert len(_decoded_token_bytes(session_id, "sess_")) == 16
    assert len(_decoded_token_bytes(command_id, "cmd_")) == 16
    assert len(_decoded_token_bytes(device_code, "pair_")) == 32
    for value in (executor_id, session_id, command_id, device_code):
        assert re.fullmatch(r"[A-Za-z0-9_-]+", value)


def test_pairing_user_code_avoids_ambiguous_glyphs() -> None:
    for _ in range(32):
        code = new_user_code()
        assert re.fullmatch(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}", code)
        assert not ({"I", "O", "0", "1"} & set(code))


def test_executor_credential_uses_verifier_and_has_no_inactivity_expiry() -> (
    None
):
    credential = new_executor_credential()
    assert len(_decoded_token_bytes(credential, "wg_exec_")) == 32
    verifier = executor_credential_verifier(credential)
    thirty_days = 30 * 24 * 60 * 60
    record = ExecutorTrustRecord(
        executor_id=new_executor_id(),
        name="laptop",
        credential_verifier=verifier,
        created_at=1_000.0,
        last_seen_at=10_000_000.0 - thirty_days,
    )

    assert executor_credential_is_trusted(record, credential)

    stale_presence = ExecutorTrustRecord(
        executor_id=record.executor_id,
        name=record.name,
        credential_verifier=record.credential_verifier,
        created_at=record.created_at,
        last_seen_at=1_000.0,
    )
    assert executor_credential_is_trusted(stale_presence, credential)

    revoked = record.model_copy(update={"revoked_at": 2_000.0})
    assert not executor_credential_is_trusted(revoked, credential)
    assert not executor_credential_is_trusted(record, new_executor_credential())


def test_pairing_models_keep_human_code_separate_from_device_secret() -> None:
    executor_id = new_executor_id()
    device_code = new_device_code()
    user_code = new_user_code()
    credential = new_executor_credential()

    start = PairStartRequest(
        requested_name="desk",
        existing_executor_id=executor_id,
        metadata=PairingExecutorMetadata(
            hostname="desk.local", platform="linux", build="5.0.0a1"
        ),
    )
    response = PairStartResponse(
        device_code=device_code,
        user_code=user_code,
        verification_uri="https://workgate.example.com/pair",
        expires_in=600,
        poll_interval=5,
    )
    approval = PairApprovalRequest(
        user_code=user_code,
        decision=PairDecision.APPROVE,
        replace_executor_id=executor_id,
    )
    poll = PairPollRequest(device_code=device_code)
    success = PairPollSuccess(executor_id=executor_id, credential=credential)

    assert start.existing_executor_id == executor_id
    assert response.device_code == poll.device_code
    assert approval.user_code == response.user_code
    assert success.executor_id == executor_id
    assert success.credential == credential


def test_protocol_rejects_old_short_session_identity() -> None:
    with pytest.raises(ValidationError):
        ExecutorCommand(
            id=new_command_id(), op="shell.run", session_id="SESSION1"
        )


def test_hello_uses_complete_thin_session_inventory() -> None:
    session_id = new_session_id()
    hello = ExecutorHelloRequest(
        protocol_version=EXECUTOR_PROTOCOL_VERSION,
        runtime=ExecutorRuntimeSummary(
            workgate_version="5.0.0a1", platform="linux-x86_64"
        ),
        capabilities=("session", "shell"),
        workspace_root="/workspace",
        sessions=(
            SessionInventorySummary(
                session_id=session_id, resolved_workdir="/workspace/project"
            ),
        ),
    )

    encoded = hello.model_dump(mode="json")
    assert encoded["protocol_version"] == 1
    assert encoded["sessions"] == [
        {"session_id": session_id, "resolved_workdir": "/workspace/project"}
    ]
    assert "executor_id" not in encoded


def test_hello_timing_requires_offline_threshold_after_heartbeat() -> None:
    response = ExecutorHelloResponse(
        heartbeat_interval_s=15,
        offline_after_s=60,
        poll_timeout_s=30,
    )
    assert response.protocol_version == 1

    with pytest.raises(ValidationError):
        ExecutorHelloResponse(
            heartbeat_interval_s=30,
            offline_after_s=30,
            poll_timeout_s=30,
        )


def test_command_envelope_is_minimal_and_executor_identity_is_not_in_body() -> (
    None
):
    command = ExecutorCommand(
        id=new_command_id(),
        op="file.write",
        session_id=new_session_id(),
        args={"path": "note.txt", "content": "hello"},
    )
    encoded = command.model_dump(mode="json")

    assert set(encoded) == {"id", "op", "session_id", "args"}
    with pytest.raises(ValidationError):
        ExecutorCommand.model_validate(
            {**encoded, "executor_id": new_executor_id()}
        )


def test_result_shape_and_unknown_command_error_are_unambiguous() -> None:
    command_id = new_command_id()
    success = ExecutorResult(id=command_id, ok=True, result={"stdout": "ok"})
    failure = ExecutorResult(
        id=command_id,
        ok=False,
        error=ProtocolError(
            code=ProtocolErrorCode.UNKNOWN_COMMAND,
            message="command is no longer pending",
        ),
    )

    assert success.error is None
    assert failure.result is None
    assert failure.error is not None
    assert failure.error.code is ProtocolErrorCode.UNKNOWN_COMMAND

    with pytest.raises(ValidationError):
        ExecutorResult(id=command_id, ok=False)
    with pytest.raises(ValidationError):
        ExecutorResult(
            id=command_id,
            ok=True,
            error=ProtocolError(
                code=ProtocolErrorCode.EXECUTOR_OFFLINE,
                message="offline",
            ),
        )


def test_protocol_error_taxonomy_is_intentionally_small() -> None:
    assert {code.value for code in ProtocolErrorCode} == {
        "unsupported_protocol",
        "unauthorized_executor",
        "executor_revoked",
        "pairing_required",
        "pairing_pending",
        "pairing_denied",
        "pairing_expired",
        "pairing_capacity_exhausted",
        "unknown_command",
        "executor_overloaded",
        "operation_unsupported",
        "session_not_found",
        "executor_offline",
    }


def test_protocol_package_has_no_runtime_or_persistence_imports() -> None:
    protocol_dir = Path(__file__).parents[1] / "src" / "workgate" / "protocol"
    forbidden_roots = {
        "fastapi",
        "httpx",
        "starlette",
        "workgate.config",
        "workgate.executors",
        "workgate.persistence",
        "workgate.remote",
        "workgate.remote_worker",
    }

    for path in protocol_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module
            ):
                imports.add(node.module)
        for imported in imports:
            assert not any(
                imported == root or imported.startswith(root + ".")
                for root in forbidden_roots
            ), f"{path.name} imports forbidden dependency {imported}"
