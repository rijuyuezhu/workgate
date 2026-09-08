from types import SimpleNamespace
from typing import Any, cast

import pytest

from workgate.ops.utils.remote_session import (
    _remote_binding,
    _remote_result_data,
    start_worker_session,
)
from workgate.tool_session.records import AgentSession


def test_remote_binding_rejects_incomplete_legacy_records() -> None:
    with pytest.raises(ValueError, match="session is not remote"):
        _remote_binding(
            cast(AgentSession, SimpleNamespace(target="local", machine=None))
        )
    with pytest.raises(RuntimeError, match="missing machine"):
        _remote_binding(
            cast(
                AgentSession,
                SimpleNamespace(
                    target="remote",
                    machine=None,
                    worker_session_id="worker-session",
                ),
            )
        )
    with pytest.raises(RuntimeError, match="missing worker_session_id"):
        _remote_binding(
            cast(
                AgentSession,
                SimpleNamespace(
                    target="remote",
                    machine="worker-a",
                    worker_session_id=None,
                ),
            )
        )


def test_remote_result_wraps_successful_scalar_data() -> None:
    assert _remote_result_data(
        {"ok": True, "data": "plain"},
        tool="read",
        machine="worker-a",
    ) == {"result": "plain"}


@pytest.mark.asyncio
async def test_start_worker_session_preserves_legacy_sessionless_start_contract() -> (
    None
):
    calls: list[tuple[str, str, dict[str, Any], int | None]] = []

    async def call_worker(
        machine: str,
        tool: str,
        args: dict[str, Any],
        timeout_s: int | None,
    ) -> dict[str, Any]:
        calls.append((machine, tool, args, timeout_s))
        return {"ok": True, "data": {"session_id": "legacy-worker-session"}}

    result = await start_worker_session(
        machine="worker-a",
        workdir="/workspace/project",
        label="legacy",
        timeout_s=9,
        call_worker=call_worker,
    )

    assert result == {"session_id": "legacy-worker-session"}
    assert calls == [
        (
            "worker-a",
            "session_start",
            {
                "target": "local",
                "workdir": "/workspace/project",
                "label": "legacy",
            },
            9,
        )
    ]
