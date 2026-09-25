from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from workgate.control.pairing import PairingAttemptView
from workgate.hosted import HostedHttpGateway, HostedHttpResponse
from workgate.hosted.http import (
    _pairing_error,
    _transport_error,
    owner_bearer_matches,
)
from workgate.hosted.mcp import (
    HOSTED_MCP_TOOL_NAMES,
    LATEST_MCP_PROTOCOL_VERSION,
    MODERN_MCP_PROTOCOL_VERSION,
    _json_type_name,
    _jsonable,
    _matches_json_type,
    _validate_json_schema,
)
from workgate.protocol.errors import ProtocolError, ProtocolErrorCode
from workgate.protocol.executor import (
    EXECUTOR_HEARTBEAT_PATH,
    EXECUTOR_HELLO_PATH,
    EXECUTOR_PAIR_POLL_PATH,
    EXECUTOR_PAIR_START_PATH,
    EXECUTOR_POLL_PATH,
    EXECUTOR_RESULT_PATH,
    EXECUTOR_VALIDATE_PATH,
)
from workgate.protocol.ids import (
    new_command_id,
    new_executor_id,
    new_session_id,
)
from workgate.protocol.pairing import PairDecision, PairingExecutorMetadata


class _Sessions:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def start_session(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("session_start", kwargs))
        return {"session_id": "ABCDEFGH", **kwargs}

    async def change_cwd(self, session_id: str, workdir: str) -> dict[str, Any]:
        self.calls.append(
            (
                "session_change_cwd",
                {"session_id": session_id, "workdir": workdir},
            )
        )
        return {"session_id": session_id, "workdir": workdir}

    async def end_session(
        self, session_id: str, *, force: bool = False
    ) -> dict[str, Any]:
        self.calls.append(
            ("session_end", {"session_id": session_id, "force": force})
        )
        return {"session_id": session_id, "ended": True, "force": force}

    async def call_session_tool(
        self, name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((name, args))
        return {"tool": name, "args": args}


class _Pairing:
    def __init__(self) -> None:
        self.decisions: list[Any] = []
        self.start_error: Exception | None = None
        self.poll_error: Exception | None = None
        self.decide_error: Exception | None = None
        self.lookup_error: Exception | None = None
        self.lookup_existing_executor_id: str | None = None

    async def start_pairing(self, request: Any) -> Any:
        if self.start_error is not None:
            raise self.start_error
        return SimpleNamespace(
            model_dump=lambda **_: {
                "device_code": "pair_" + "a" * 43,
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://control.example/pair",
                "expires_in": 300,
                "poll_interval": 2,
                "requested_name": request.requested_name,
            }
        )

    async def poll(self, device_code: str) -> Any:
        if self.poll_error is not None:
            raise self.poll_error
        return SimpleNamespace(
            model_dump=lambda **_: {
                "executor_id": "exec_" + "a" * 22,
                "credential": "credential",
                "device_code": device_code,
            }
        )

    async def decide(self, request: Any) -> PairingAttemptView:
        if self.decide_error is not None:
            raise self.decide_error
        self.decisions.append(request)
        return PairingAttemptView(
            user_code=str(request.user_code),
            requested_name=None,
            existing_executor_id=None,
            metadata=PairingExecutorMetadata(),
            expires_in=300,
            status="approved",
            executor_id="exec_test",
            name="laptop",
        )

    async def lookup_user_code(self, user_code: str) -> PairingAttemptView:
        if self.lookup_error is not None:
            raise self.lookup_error
        return PairingAttemptView(
            user_code=user_code,
            requested_name="laptop",
            existing_executor_id=self.lookup_existing_executor_id,
            metadata=PairingExecutorMetadata(
                hostname="host",
                platform="linux",
                build="test",
            ),
            expires_in=300,
            status="pending",
        )


class _ControlState:
    def __init__(self) -> None:
        self.executors: dict[str, Any] = {}
        self.sessions: dict[str, Any] = {}

    def snapshot_executors(self) -> dict[str, Any]:
        return dict(self.executors)

    def snapshot_sessions(self) -> dict[str, Any]:
        return dict(self.sessions)


class _Transport:
    def __init__(self) -> None:
        self.online = False
        self.errors: dict[str, Exception] = {}
        self.calls: list[tuple[str, Any]] = []
        self.poll_result: Any | None = None

    def _raise(self, operation: str) -> None:
        error = self.errors.get(operation)
        if error is not None:
            raise error

    async def is_online(self, _executor_id: str) -> bool:
        return self.online

    async def hello(self, credential: str, request: Any) -> Any:
        self._raise("hello")
        self.calls.append(("hello", (credential, request)))
        return SimpleNamespace(
            model_dump=lambda **_: {
                "protocol_version": 1,
                "heartbeat_interval_s": 15,
                "offline_after_s": 45,
                "poll_timeout_s": 25,
            }
        )

    async def validate(self, credential: str) -> None:
        self._raise("validate")
        self.calls.append(("validate", credential))

    async def heartbeat(self, credential: str) -> None:
        self._raise("heartbeat")
        self.calls.append(("heartbeat", credential))

    async def poll(self, credential: str) -> Any | None:
        self._raise("poll")
        self.calls.append(("poll", credential))
        return self.poll_result

    async def submit_result(self, credential: str, result: Any) -> None:
        self._raise("result")
        self.calls.append(("result", (credential, result)))


class _ProtocolFailure(RuntimeError):
    def __init__(self, code: ProtocolErrorCode) -> None:
        super().__init__(code.value)
        self.error = ProtocolError(code=code, message=f"{code.value} test")


def _actor() -> Any:
    return SimpleNamespace(
        session_coordinator=_Sessions(),
        executor_pairing=_Pairing(),
        control_state=_ControlState(),
        executor_transport=_Transport(),
    )


def _headers(token: str = "x" * 32) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _modern_headers(
    method: str,
    *,
    name: str | None = None,
    version: str = MODERN_MCP_PROTOCOL_VERSION,
) -> dict[str, str]:
    headers = {
        **_headers(),
        "MCP-Protocol-Version": version,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


def _modern_meta(
    *, version: str = MODERN_MCP_PROTOCOL_VERSION
) -> dict[str, Any]:
    return {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {
            "name": "test-client",
            "version": "1",
        },
    }


def _json_body(response: HostedHttpResponse) -> dict[str, Any]:
    assert isinstance(response.body, dict)
    return cast(dict[str, Any], response.body)


def _text_body(response: HostedHttpResponse) -> str:
    assert isinstance(response.body, str)
    return response.body


def test_hosted_tool_surface_is_explicit_and_excludes_unadapted_features() -> (
    None
):
    assert {
        "session_start",
        "session_change_cwd",
        "session_end",
        "read",
        "bash",
        "write_file",
        "search",
        "workspace_search",
    } <= HOSTED_MCP_TOOL_NAMES
    assert {
        "session_copy",
        "job",
        "audit_tail",
        "read_todos",
        "write_todos",
        "create_file_link",
        "view_image",
        "call_agent_mcp_tool",
    }.isdisjoint(HOSTED_MCP_TOOL_NAMES)


def test_hosted_gateway_requires_strong_owner_token() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        HostedHttpGateway(_actor(), owner_token="short")


def test_hosted_owner_bearer_match_is_case_insensitive_and_exact() -> None:
    token = "x" * 32
    assert owner_bearer_matches({"Authorization": f"Bearer {token}"}, token)
    assert not owner_bearer_matches(
        {"authorization": f"Bearer {token}y"}, token
    )
    assert not owner_bearer_matches({"authorization": token}, token)


@pytest.mark.asyncio
async def test_hosted_mcp_requires_owner_bearer_and_lists_generated_tools() -> (
    None
):
    gateway = HostedHttpGateway(_actor(), owner_token="x" * 32)

    denied = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers={},
        payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert denied.status == 401

    initialized = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_headers(),
        payload={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        },
    )
    assert initialized.status == 200
    initialized_body = _json_body(initialized)
    assert initialized_body["result"]["protocolVersion"] == "2025-11-25"

    listed = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_headers(),
        payload={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    listed_body = _json_body(listed)
    names = {row["name"] for row in listed_body["result"]["tools"]}
    assert names == HOSTED_MCP_TOOL_NAMES


@pytest.mark.asyncio
async def test_hosted_mcp_2026_discovery_and_tool_listing() -> None:
    gateway = HostedHttpGateway(_actor(), owner_token="x" * 32)
    discovered = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_modern_headers("server/discover"),
        payload={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "server/discover",
            "params": {"_meta": _modern_meta()},
        },
    )
    assert discovered.status == 200
    result = _json_body(discovered)["result"]
    assert result["supportedVersions"] == [MODERN_MCP_PROTOCOL_VERSION]
    assert result["resultType"] == "complete"
    assert result["cacheScope"] == "private"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert (
        result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"]
        == "workgate-hosted"
    )

    listed = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_modern_headers("tools/list"),
        payload={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {"_meta": _modern_meta()},
        },
    )
    assert listed.status == 200
    listed_result = _json_body(listed)["result"]
    assert listed_result["resultType"] == "complete"
    assert listed_result["ttlMs"] == 0
    assert {tool["name"] for tool in listed_result["tools"]} == (
        HOSTED_MCP_TOOL_NAMES
    )


@pytest.mark.asyncio
async def test_hosted_mcp_2026_tool_call_routes_with_modern_result() -> None:
    actor = _actor()
    gateway = HostedHttpGateway(actor, owner_token="x" * 32)
    response = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_modern_headers("tools/call", name="bash"),
        payload={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "_meta": _modern_meta(),
                "name": "bash",
                "arguments": {
                    "session_id": "ABCDEFGH",
                    "command": "pwd",
                },
            },
        },
    )
    assert response.status == 200
    result = _json_body(response)["result"]
    assert result["resultType"] == "complete"
    assert result["isError"] is False
    assert "ttlMs" not in result
    assert "cacheScope" not in result
    assert result["structuredContent"]["tool"] == "bash"
    assert actor.session_coordinator.calls[-1][0] == "bash"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "params", "code"),
    [
        (
            _modern_headers("tools/list"),
            {},
            -32602,
        ),
        (
            _modern_headers("resources/list"),
            {"_meta": _modern_meta()},
            -32020,
        ),
        (
            _modern_headers("tools/list", version="2099-01-01"),
            {"_meta": _modern_meta(version="2099-01-01")},
            -32022,
        ),
    ],
)
async def test_hosted_mcp_2026_rejects_invalid_envelopes(
    headers: dict[str, str],
    params: dict[str, Any],
    code: int,
) -> None:
    gateway = HostedHttpGateway(_actor(), owner_token="x" * 32)
    response = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=headers,
        payload={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/list",
            "params": params,
        },
    )
    assert response.status == 400
    assert _json_body(response)["error"]["code"] == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "headers"),
    [
        (
            "tools/list",
            {"_meta": _modern_meta(), "cursor": "next"},
            _modern_headers("tools/list"),
        ),
        (
            "tools/call",
            {
                "_meta": _modern_meta(),
                "name": "bash",
                "arguments": {
                    "session_id": "ABCDEFGH",
                    "command": "pwd",
                },
                "requestState": "resume-me",
            },
            _modern_headers("tools/call", name="bash"),
        ),
        (
            "tools/call",
            {
                "_meta": _modern_meta(),
                "name": "bash",
                "arguments": {
                    "session_id": "ABCDEFGH",
                    "command": "pwd",
                },
                "inputResponses": {},
            },
            _modern_headers("tools/call", name="bash"),
        ),
        (
            "tools/call",
            {
                "_meta": _modern_meta(),
                "name": "bash",
                "arguments": {
                    "session_id": "ABCDEFGH",
                    "command": "pwd",
                },
                "task": {},
            },
            _modern_headers("tools/call", name="bash"),
        ),
    ],
)
async def test_hosted_mcp_2026_rejects_unimplemented_extensions(
    method: str,
    params: dict[str, Any],
    headers: dict[str, str],
) -> None:
    gateway = HostedHttpGateway(_actor(), owner_token="x" * 32)
    response = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=headers,
        payload={
            "jsonrpc": "2.0",
            "id": 41,
            "method": method,
            "params": params,
        },
    )
    assert response.status == 400
    body = _json_body(response)
    assert body["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_hosted_mcp_2026_rejects_name_header_mismatch() -> None:
    gateway = HostedHttpGateway(_actor(), owner_token="x" * 32)
    response = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_modern_headers("tools/call", name="read"),
        payload={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "_meta": _modern_meta(),
                "name": "bash",
                "arguments": {
                    "session_id": "ABCDEFGH",
                    "command": "pwd",
                },
            },
        },
    )
    assert response.status == 400
    assert _json_body(response)["error"]["code"] == -32020


@pytest.mark.asyncio
async def test_hosted_mcp_2026_client_info_is_optional_but_validated() -> None:
    gateway = HostedHttpGateway(_actor(), owner_token="x" * 32)
    meta = _modern_meta()
    meta.pop("io.modelcontextprotocol/clientInfo")
    accepted = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_modern_headers("tools/list"),
        payload={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/list",
            "params": {"_meta": meta},
        },
    )
    assert accepted.status == 200

    meta["io.modelcontextprotocol/clientInfo"] = {"name": "broken"}
    rejected = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_modern_headers("tools/list"),
        payload={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/list",
            "params": {"_meta": meta},
        },
    )
    assert rejected.status == 400
    assert _json_body(rejected)["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_hosted_mcp_2026_does_not_serve_removed_ping() -> None:
    gateway = HostedHttpGateway(_actor(), owner_token="x" * 32)
    response = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_modern_headers("ping"),
        payload={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "ping",
            "params": {"_meta": _modern_meta()},
        },
    )
    assert response.status == 404
    assert _json_body(response)["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_hosted_mcp_2026_acknowledges_and_drops_notifications() -> None:
    gateway = HostedHttpGateway(_actor(), owner_token="x" * 32)
    response = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers={
            **_headers(),
            "MCP-Protocol-Version": MODERN_MCP_PROTOCOL_VERSION,
            "Mcp-Method": "notifications/cancelled",
        },
        payload={
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 1, "reason": "client closed"},
        },
    )
    assert response.status == 202
    assert response.body is None


@pytest.mark.asyncio
async def test_hosted_mcp_2026_notification_rejects_unsupported_version() -> (
    None
):
    gateway = HostedHttpGateway(_actor(), owner_token="x" * 32)
    response = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers={
            **_headers(),
            "MCP-Protocol-Version": "2099-01-01",
            "Mcp-Method": "notifications/cancelled",
        },
        payload={
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {},
        },
    )
    assert response.status == 400
    assert _json_body(response)["error"]["code"] == -32022


@pytest.mark.asyncio
async def test_hosted_mcp_routes_sessions_and_machine_tools() -> None:
    actor = _actor()
    gateway = HostedHttpGateway(actor, owner_token="x" * 32)

    started = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_headers(),
        payload={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "session_start",
                "arguments": {"workdir": "/workspace", "label": "hosted"},
            },
        },
    )
    started_body = _json_body(started)
    assert started_body["result"]["isError"] is False
    assert (
        started_body["result"]["structuredContent"]["workdir"] == "/workspace"
    )

    bash = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_headers(),
        payload={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "bash",
                "arguments": {
                    "session_id": "ABCDEFGH",
                    "command": "pwd",
                },
            },
        },
    )
    bash_body = _json_body(bash)
    assert bash_body["result"]["structuredContent"] == {
        "tool": "bash",
        "args": {"session_id": "ABCDEFGH", "command": "pwd"},
    }
    assert actor.session_coordinator.calls[-1][0] == "bash"


@pytest.mark.asyncio
async def test_hosted_pair_page_is_public_but_decision_requires_owner_bearer() -> (
    None
):
    actor = _actor()
    gateway = HostedHttpGateway(actor, owner_token="x" * 32)

    page = await gateway.dispatch(method="GET", path="/pair", headers={})
    assert page.status == 200
    assert page.content_type.startswith("text/html")
    page_body = _text_body(page)
    assert "Pair an executor" in page_body
    assert "Review request" in page_body
    assert "Replace the existing executor credential" in page_body
    assert "/pair/lookup" in page_body
    assert "x" * 32 not in page_body

    lookup_denied = await gateway.dispatch(
        method="POST",
        path="/pair/lookup",
        headers={},
        payload={"user_code": "ABCD-EFGH"},
    )
    assert lookup_denied.status == 401

    existing_id = new_executor_id()
    actor.executor_pairing.lookup_existing_executor_id = existing_id
    lookup = await gateway.dispatch(
        method="POST",
        path="/pair/lookup",
        headers=_headers(),
        payload={"user_code": "ABCD-EFGH"},
    )
    assert lookup.status == 200
    lookup_body = _json_body(lookup)
    assert lookup_body["requested_name"] == "laptop"
    assert lookup_body["existing_executor_id"] == existing_id
    assert lookup_body["metadata"] == {
        "hostname": "host",
        "platform": "linux",
        "build": "test",
    }

    invalid_lookup = await gateway.dispatch(
        method="POST",
        path="/pair/lookup",
        headers=_headers(),
        payload={"user_code": "bad"},
    )
    assert invalid_lookup.status == 422

    denied = await gateway.dispatch(
        method="POST",
        path="/pair",
        headers={},
        payload={"user_code": "ABCD-EFGH", "decision": "approve"},
    )
    assert denied.status == 401

    approved = await gateway.dispatch(
        method="POST",
        path="/pair",
        headers=_headers(),
        payload={
            "user_code": "ABCD-EFGH",
            "decision": "approve",
            "replace_executor_id": existing_id,
        },
    )
    assert approved.status == 200
    assert _json_body(approved)["status"] == "approved"
    assert actor.executor_pairing.decisions[0].decision is PairDecision.APPROVE
    assert (
        actor.executor_pairing.decisions[0].replace_executor_id == existing_id
    )

    actor.executor_pairing.lookup_error = _ProtocolFailure(
        ProtocolErrorCode.PAIRING_REQUIRED
    )
    missing_lookup = await gateway.dispatch(
        method="POST",
        path="/pair/lookup",
        headers=_headers(),
        payload={"user_code": "ABCD-EFGH"},
    )
    assert missing_lookup.status == 404


@pytest.mark.asyncio
async def test_hosted_mcp_unknown_tool_is_error_result_not_dispatch() -> None:
    gateway = HostedHttpGateway(_actor(), owner_token="x" * 32)
    response = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_headers(),
        payload={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "session_copy", "arguments": {}},
        },
    )
    assert response.status == 200
    body = _json_body(response)
    assert body["result"]["isError"] is True
    assert "unsupported hosted MCP tool" in body["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_hosted_mcp_rejects_background_job_mode() -> None:
    gateway = HostedHttpGateway(_actor(), owner_token="x" * 32)
    response = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_headers(),
        payload={
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "bash",
                "arguments": {
                    "session_id": "ABCDEFGH",
                    "command": "sleep 1",
                    "async_": True,
                },
            },
        },
    )
    assert response.status == 200
    body = _json_body(response)
    assert body["result"]["isError"] is True
    assert "background job creation" in body["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_hosted_mcp_allows_pty_when_async_flag_is_also_true() -> None:
    actor = _actor()
    gateway = HostedHttpGateway(actor, owner_token="x" * 32)
    response = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_headers(),
        payload={
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "bash",
                "arguments": {
                    "session_id": "ABCDEFGH",
                    "command": "bash",
                    "async_": True,
                    "pty": True,
                },
            },
        },
    )
    assert response.status == 200
    assert _json_body(response)["result"]["isError"] is False
    assert actor.session_coordinator.calls[-1] == (
        "bash",
        {
            "session_id": "ABCDEFGH",
            "command": "bash",
            "async_": True,
            "pty": True,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("session_start", {"workdir": 7}, "expected string"),
        (
            "bash",
            {"session_id": "ABCDEFGH", "command": "pwd", "timeout_s": "10"},
            "no allowed schema matched",
        ),
        (
            "search",
            {"session_id": "ABCDEFGH", "pattern": "x", "paths": ["src", 9]},
            "no allowed schema matched",
        ),
    ],
)
async def test_hosted_mcp_validates_generated_tool_arguments(
    name: str, arguments: dict[str, Any], expected: str
) -> None:
    actor = _actor()
    gateway = HostedHttpGateway(actor, owner_token="x" * 32)
    response = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_headers(),
        payload={
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status == 200
    body = _json_body(response)
    assert body["result"]["isError"] is True
    assert expected in body["result"]["content"][0]["text"]
    assert actor.session_coordinator.calls == []


@pytest.mark.asyncio
async def test_hosted_misc_routes_and_status_projection() -> None:
    actor = _actor()
    executor_id = new_executor_id()
    session_id = new_session_id()
    actor.control_state.executors[executor_id] = SimpleNamespace(
        executor_id=executor_id,
        name="laptop",
        revoked_at=None,
    )
    actor.control_state.sessions[session_id] = SimpleNamespace(
        session_id=session_id,
        executor_id=executor_id,
        status="active",
        resolved_workdir_display="/workspace/project",
    )
    actor.executor_transport.online = True
    gateway = HostedHttpGateway(actor, owner_token="x" * 32)

    health = await gateway.dispatch(method="get", path="/healthz/", headers={})
    assert health.status == 200
    assert _json_body(health) == {"ok": True}

    denied = await gateway.dispatch(method="GET", path="/status", headers={})
    assert denied.status == 401

    status = await gateway.dispatch(
        method="GET",
        path="/status",
        headers={"Authorization": f"Bearer {'x' * 32}"},
    )
    body = _json_body(status)
    assert body["executors"] == [
        {
            "executor_id": executor_id,
            "name": "laptop",
            "online": True,
            "revoked": False,
        }
    ]
    assert body["sessions"][0]["session_id"] == session_id
    assert body["sessions"][0]["workdir"] == "/workspace/project"

    for method in ("GET", "DELETE", "PUT"):
        response = await gateway.dispatch(
            method=method,
            path="/mcp",
            headers=_headers(),
        )
        assert response.status == 405

    missing = await gateway.dispatch(method="GET", path="/missing", headers={})
    assert missing.status == 404


@pytest.mark.asyncio
async def test_hosted_pair_start_poll_and_validation_errors() -> None:
    actor = _actor()
    gateway = HostedHttpGateway(actor, owner_token="x" * 32)

    started = await gateway.dispatch(
        method="POST",
        path=EXECUTOR_PAIR_START_PATH,
        headers={},
        payload={"requested_name": "laptop"},
    )
    assert started.status == 200
    assert _json_body(started)["requested_name"] == "laptop"

    polled = await gateway.dispatch(
        method="POST",
        path=EXECUTOR_PAIR_POLL_PATH,
        headers={},
        payload={"device_code": "pair_" + "a" * 43},
    )
    assert polled.status == 200
    assert _json_body(polled)["credential"] == "credential"

    invalid_start = await gateway.dispatch(
        method="POST",
        path=EXECUTOR_PAIR_START_PATH,
        headers={},
        payload={"requested_name": ""},
    )
    assert invalid_start.status == 422
    invalid_poll = await gateway.dispatch(
        method="POST",
        path=EXECUTOR_PAIR_POLL_PATH,
        headers={},
        payload={"device_code": "bad"},
    )
    assert invalid_poll.status == 422
    invalid_decision = await gateway.dispatch(
        method="POST",
        path="/pair",
        headers=_headers(),
        payload={"user_code": "bad", "decision": "approve"},
    )
    assert invalid_decision.status == 422


@pytest.mark.asyncio
async def test_hosted_pairing_protocol_errors_and_unexpected_failures() -> None:
    actor = _actor()
    gateway = HostedHttpGateway(actor, owner_token="x" * 32)
    actor.executor_pairing.start_error = _ProtocolFailure(
        ProtocolErrorCode.PAIRING_CAPACITY_EXHAUSTED
    )
    response = await gateway.dispatch(
        method="POST",
        path=EXECUTOR_PAIR_START_PATH,
        headers={},
        payload={},
    )
    assert response.status == 429

    actor.executor_pairing.start_error = RuntimeError("unexpected")
    with pytest.raises(RuntimeError, match="unexpected"):
        await gateway.dispatch(
            method="POST",
            path=EXECUTOR_PAIR_START_PATH,
            headers={},
            payload={},
        )


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (ProtocolErrorCode.PAIRING_PENDING, 202),
        (ProtocolErrorCode.PAIRING_DENIED, 403),
        (ProtocolErrorCode.PAIRING_EXPIRED, 410),
        (ProtocolErrorCode.PAIRING_CAPACITY_EXHAUSTED, 429),
        (ProtocolErrorCode.OPERATION_UNSUPPORTED, 400),
    ],
)
def test_hosted_pairing_error_status_mapping(
    code: ProtocolErrorCode, status: int
) -> None:
    response = _pairing_error(_ProtocolFailure(code))
    assert response is not None
    assert response.status == status
    assert _json_body(response)["error"]["code"] == code.value
    assert _pairing_error(RuntimeError("plain")) is None


def _hello_payload() -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "runtime": {"workgate_version": "test"},
        "capabilities": ["sessions.v1"],
        "workspace_root": "/workspace",
        "sessions": [],
        "shells": [],
        "jobs": [],
    }


@pytest.mark.asyncio
async def test_hosted_executor_protocol_success_paths() -> None:
    actor = _actor()
    gateway = HostedHttpGateway(actor, owner_token="x" * 32)
    headers = {"authorization": "Bearer executor-credential"}

    hello = await gateway.dispatch(
        method="POST",
        path=EXECUTOR_HELLO_PATH,
        headers=headers,
        payload=_hello_payload(),
    )
    assert hello.status == 200
    assert _json_body(hello)["protocol_version"] == 1

    validate = await gateway.dispatch(
        method="POST",
        path=EXECUTOR_VALIDATE_PATH,
        headers=headers,
        payload=None,
    )
    assert validate.status == 204
    heartbeat = await gateway.dispatch(
        method="POST",
        path=EXECUTOR_HEARTBEAT_PATH,
        headers=headers,
        payload=None,
    )
    assert heartbeat.status == 204
    empty_poll = await gateway.dispatch(
        method="POST",
        path=EXECUTOR_POLL_PATH,
        headers=headers,
        payload=None,
    )
    assert empty_poll.status == 204

    command_id = new_command_id()
    actor.executor_transport.poll_result = SimpleNamespace(
        model_dump=lambda **_: {
            "id": command_id,
            "op": "shell.run",
            "session_id": None,
            "args": {"command": "true"},
        }
    )
    command = await gateway.dispatch(
        method="POST",
        path=EXECUTOR_POLL_PATH,
        headers=headers,
        payload=None,
    )
    assert command.status == 200
    assert _json_body(command)["id"] == command_id

    result = await gateway.dispatch(
        method="POST",
        path=EXECUTOR_RESULT_PATH,
        headers=headers,
        payload={"id": command_id, "ok": True, "result": {"done": True}},
    )
    assert result.status == 204

    invalid = await gateway.dispatch(
        method="POST",
        path=EXECUTOR_HELLO_PATH,
        headers=headers,
        payload={},
    )
    assert invalid.status == 422


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (ProtocolErrorCode.UNAUTHORIZED_EXECUTOR, 401),
        (ProtocolErrorCode.EXECUTOR_REVOKED, 403),
        (ProtocolErrorCode.UNKNOWN_COMMAND, 404),
        (ProtocolErrorCode.EXECUTOR_OVERLOADED, 409),
        (ProtocolErrorCode.OPERATION_UNSUPPORTED, 400),
    ],
)
def test_hosted_transport_error_status_mapping(
    code: ProtocolErrorCode, status: int
) -> None:
    response = _transport_error(_ProtocolFailure(code))
    assert response is not None
    assert response.status == status
    assert _json_body(response)["error"]["code"] == code.value
    assert _transport_error(RuntimeError("plain")) is None


@pytest.mark.asyncio
async def test_hosted_executor_protocol_error_and_unexpected_failure() -> None:
    actor = _actor()
    gateway = HostedHttpGateway(actor, owner_token="x" * 32)
    actor.executor_transport.errors["validate"] = _ProtocolFailure(
        ProtocolErrorCode.UNAUTHORIZED_EXECUTOR
    )
    denied = await gateway.dispatch(
        method="POST",
        path=EXECUTOR_VALIDATE_PATH,
        headers={"authorization": "Bearer bad"},
    )
    assert denied.status == 401

    actor.executor_transport.errors["validate"] = RuntimeError("unexpected")
    with pytest.raises(RuntimeError, match="unexpected"):
        await gateway.dispatch(
            method="POST",
            path=EXECUTOR_VALIDATE_PATH,
            headers={"authorization": "Bearer bad"},
        )


@pytest.mark.asyncio
async def test_hosted_mcp_jsonrpc_boundary_cases() -> None:
    gateway = HostedHttpGateway(_actor(), owner_token="x" * 32)

    invalid = await gateway.dispatch(
        method="POST", path="/mcp", headers=_headers(), payload=[]
    )
    assert invalid.status == 400
    assert _json_body(invalid)["error"]["code"] == -32600

    for payload in (
        {"id": 1, "method": "ping"},
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1, "method": 7},
        {"jsonrpc": "2.0", "id": None, "method": "ping"},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
        {"jsonrpc": "2.0", "id": [], "method": "ping"},
    ):
        malformed = await gateway.dispatch(
            method="POST", path="/mcp", headers=_headers(), payload=payload
        )
        assert malformed.status == 400
        assert _json_body(malformed)["error"]["code"] == -32600

    for payload in (
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/other"},
    ):
        notification = await gateway.dispatch(
            method="POST", path="/mcp", headers=_headers(), payload=payload
        )
        assert notification.status == 202
        assert notification.body is None

    initialized = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_headers(),
        payload={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "unsupported"},
        },
    )
    assert (
        _json_body(initialized)["result"]["protocolVersion"]
        == LATEST_MCP_PROTOCOL_VERSION
    )

    ping = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_headers(),
        payload={"jsonrpc": "2.0", "id": 2, "method": "ping"},
    )
    assert _json_body(ping)["result"] == {}

    for params in (None, {"name": 3, "arguments": {}}):
        bad_call = await gateway.dispatch(
            method="POST",
            path="/mcp",
            headers=_headers(),
            payload={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": params,
            },
        )
        assert _json_body(bad_call)["error"]["code"] == -32602

    missing = await gateway.dispatch(
        method="POST",
        path="/mcp",
        headers=_headers(),
        payload={"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
    )
    assert _json_body(missing)["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_hosted_mcp_session_change_and_end_routes() -> None:
    actor = _actor()
    gateway = HostedHttpGateway(actor, owner_token="x" * 32)
    changed = await gateway._mcp.call_tool(
        "session_change_cwd",
        {"session_id": "ABCDEFGH", "workdir": "src"},
    )
    assert changed["workdir"] == "src"
    ended = await gateway._mcp.call_tool(
        "session_end", {"session_id": "ABCDEFGH", "force": True}
    )
    assert ended["ended"] is True
    assert ended["force"] is True


@pytest.mark.parametrize(
    ("value", "schema", "message"),
    [
        ({}, [], "invalid hosted schema"),
        ({}, {"type": "object", "required": ["x"]}, "missing required"),
        (
            {"extra": 1},
            {"type": "object", "additionalProperties": False},
            "unexpected property",
        ),
        (
            {"x": "bad"},
            {
                "type": "object",
                "additionalProperties": {"type": "integer"},
            },
            "expected integer",
        ),
        (
            [1, "bad"],
            {"type": "array", "items": {"type": "integer"}},
            "expected integer",
        ),
        ("a", {"type": "string", "minLength": 2}, "shorter"),
        ("abcd", {"type": "string", "maxLength": 3}, "longer"),
        ("abc", {"type": "string", "pattern": r"^z"}, "pattern"),
        (0, {"type": "number", "minimum": 1}, "below"),
        (3, {"type": "number", "maximum": 2}, "above"),
        ("x", {"anyOf": [{"type": "integer"}]}, "no allowed schema"),
    ],
)
def test_hosted_schema_validator_rejects_boundary_values(
    value: Any, schema: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _validate_json_schema(value, schema, path="value")


def test_hosted_schema_type_helpers_cover_supported_types() -> None:
    assert _matches_json_type(None, "null")
    assert _matches_json_type(True, "boolean")
    assert _matches_json_type("x", "string")
    assert _matches_json_type({}, "object")
    assert _matches_json_type([], "array")
    assert _matches_json_type(1.0, "integer")
    assert not _matches_json_type(1.5, "integer")
    assert _matches_json_type(1.5, "number")
    with pytest.raises(ValueError, match="unsupported"):
        _matches_json_type("x", "mystery")

    assert _json_type_name(None) == "null"
    assert _json_type_name(True) == "boolean"
    assert _json_type_name("x") == "string"
    assert _json_type_name({}) == "object"
    assert _json_type_name([]) == "array"
    assert _json_type_name(1) == "number"
    assert _json_type_name(object()) == "object"

    error = ProtocolError(
        code=ProtocolErrorCode.UNKNOWN_COMMAND, message="unknown"
    )
    dumped = _jsonable(error)
    assert dumped["code"] == ProtocolErrorCode.UNKNOWN_COMMAND.value
