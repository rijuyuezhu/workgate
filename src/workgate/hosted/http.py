"""Provider-neutral HTTP routing for the first hosted Workgate surface."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import TypeAdapter, ValidationError

from ..protocol.errors import (
    ProtocolError,
    ProtocolErrorCode,
    ProtocolErrorResponse,
)
from ..protocol.executor import (
    EXECUTOR_HEARTBEAT_PATH,
    EXECUTOR_HELLO_PATH,
    EXECUTOR_PAIR_POLL_PATH,
    EXECUTOR_PAIR_START_PATH,
    EXECUTOR_POLL_PATH,
    EXECUTOR_RESULT_PATH,
    EXECUTOR_VALIDATE_PATH,
    ExecutorHelloRequest,
    ExecutorResult,
)
from ..protocol.ids import UserCode
from ..protocol.pairing import (
    PairApprovalRequest,
    PairPollRequest,
    PairStartRequest,
)
from .actor import HostedControlActorCore
from .mcp import HostedMcpGateway

_USER_CODE_ADAPTER = TypeAdapter(UserCode)


class _PairingMetadataView(Protocol):
    def model_dump(self, *, mode: str) -> dict[str, object]: ...


class _PairingAttemptView(Protocol):
    @property
    def user_code(self) -> str: ...

    @property
    def requested_name(self) -> str | None: ...

    @property
    def existing_executor_id(self) -> str | None: ...

    @property
    def metadata(self) -> _PairingMetadataView: ...

    @property
    def expires_in(self) -> int: ...

    @property
    def status(self) -> str: ...

    @property
    def executor_id(self) -> str | None: ...

    @property
    def name(self) -> str | None: ...


@dataclass(frozen=True)
class HostedHttpResponse:
    """Small response value converted by provider-specific Worker glue."""

    status: int
    body: object | None = None
    content_type: str = "application/json"


class HostedHttpGateway:
    """Route the supported hosted HTTP/MCP/executor surface."""

    def __init__(
        self,
        actor: HostedControlActorCore,
        *,
        owner_token: str,
    ) -> None:
        if len(owner_token) < 32:
            raise ValueError(
                "hosted owner token must be at least 32 characters"
            )
        self._actor = actor
        self._owner_token = owner_token
        self._mcp = HostedMcpGateway(actor)

    async def dispatch(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        payload: object | None = None,
    ) -> HostedHttpResponse:
        """Dispatch one already-bounded provider request."""
        method = method.upper()
        path = path.rstrip("/") or "/"

        if method == "GET" and path == "/healthz":
            return HostedHttpResponse(200, {"ok": True})

        if method == "GET" and path == "/pair":
            return HostedHttpResponse(
                200, _PAIR_PAGE, content_type="text/html; charset=utf-8"
            )

        if path == EXECUTOR_PAIR_START_PATH and method == "POST":
            return await self._pair_start(payload)
        if path == EXECUTOR_PAIR_POLL_PATH and method == "POST":
            return await self._pair_poll(payload)

        if (
            path
            in {
                EXECUTOR_HELLO_PATH,
                EXECUTOR_VALIDATE_PATH,
                EXECUTOR_HEARTBEAT_PATH,
                EXECUTOR_POLL_PATH,
                EXECUTOR_RESULT_PATH,
            }
            and method == "POST"
        ):
            return await self._executor_request(path, headers, payload)

        if path == "/pair" and method == "POST":
            if not self._owner_authorized(headers):
                return _unauthorized()
            return await self._pair_decide(payload)

        if path == "/pair/lookup" and method == "POST":
            if not self._owner_authorized(headers):
                return _unauthorized()
            return await self._pair_lookup(payload)

        if path == "/status" and method == "GET":
            if not self._owner_authorized(headers):
                return _unauthorized()
            return await self._status()

        if path == "/mcp":
            if not self._owner_authorized(headers):
                return _unauthorized()
            if method in {"GET", "DELETE"}:
                return HostedHttpResponse(
                    405,
                    {"detail": "hosted MCP is stateless POST-only"},
                )
            if method != "POST":
                return HostedHttpResponse(405, {"detail": "method not allowed"})
            status, body = await self._mcp.handle_jsonrpc(
                payload, headers=headers
            )
            return HostedHttpResponse(status, body)

        return HostedHttpResponse(404, {"detail": "not found"})

    def _owner_authorized(self, headers: Mapping[str, str]) -> bool:
        return owner_bearer_matches(headers, self._owner_token)

    async def _pair_start(self, payload: object) -> HostedHttpResponse:
        try:
            request = PairStartRequest.model_validate(payload)
            result = await self._actor.executor_pairing.start_pairing(request)
        except ValidationError, TypeError, ValueError:
            return HostedHttpResponse(
                422, {"detail": "invalid executor pair-start request"}
            )
        except Exception as exc:
            response = _pairing_error(exc)
            if response is None:
                raise
            return response
        return HostedHttpResponse(200, result.model_dump(mode="json"))

    async def _pair_poll(self, payload: object) -> HostedHttpResponse:
        try:
            request = PairPollRequest.model_validate(payload)
            result = await self._actor.executor_pairing.poll(
                str(request.device_code)
            )
        except ValidationError, TypeError, ValueError:
            return HostedHttpResponse(
                422, {"detail": "invalid executor pair-poll request"}
            )
        except Exception as exc:
            response = _pairing_error(exc)
            if response is None:
                raise
            return response
        return HostedHttpResponse(200, result.model_dump(mode="json"))

    async def _pair_decide(self, payload: object) -> HostedHttpResponse:
        try:
            request = PairApprovalRequest.model_validate(payload)
            view = await self._actor.executor_pairing.decide(request)
        except ValidationError, TypeError, ValueError:
            return HostedHttpResponse(
                422, {"detail": "invalid pairing decision"}
            )
        except Exception as exc:
            response = _pairing_error(exc)
            if response is None:
                raise
            return response
        return HostedHttpResponse(200, _pairing_view(view))

    async def _pair_lookup(self, payload: object) -> HostedHttpResponse:
        try:
            if not isinstance(payload, dict):
                raise ValueError("pairing lookup payload must be an object")
            user_code = _USER_CODE_ADAPTER.validate_python(
                payload.get("user_code")
            )
            view = await self._actor.executor_pairing.lookup_user_code(
                str(user_code)
            )
        except ValidationError, TypeError, ValueError:
            return HostedHttpResponse(422, {"detail": "invalid pairing lookup"})
        except Exception as exc:
            response = _pairing_error(exc)
            if response is None:
                raise
            return response
        return HostedHttpResponse(200, _pairing_view(view))

    async def _executor_request(
        self,
        path: str,
        headers: Mapping[str, str],
        payload: object,
    ) -> HostedHttpResponse:
        credential = _bearer(headers)
        transport = self._actor.executor_transport
        try:
            if path == EXECUTOR_HELLO_PATH:
                message = ExecutorHelloRequest.model_validate(payload)
                result = await transport.hello(credential, message)
                return HostedHttpResponse(200, result.model_dump(mode="json"))
            if path == EXECUTOR_VALIDATE_PATH:
                await transport.validate(credential)
                return HostedHttpResponse(204)
            if path == EXECUTOR_HEARTBEAT_PATH:
                await transport.heartbeat(credential)
                return HostedHttpResponse(204)
            if path == EXECUTOR_POLL_PATH:
                command = await transport.poll(credential)
                if command is None:
                    return HostedHttpResponse(204)
                return HostedHttpResponse(200, command.model_dump(mode="json"))
            if path == EXECUTOR_RESULT_PATH:
                result = ExecutorResult.model_validate(payload)
                await transport.submit_result(credential, result)
                return HostedHttpResponse(204)
        except ValidationError, TypeError, ValueError:
            return HostedHttpResponse(
                422, {"detail": "invalid executor request"}
            )
        except Exception as exc:
            response = _transport_error(exc)
            if response is None:
                raise
            return response
        return HostedHttpResponse(404, {"detail": "not found"})

    async def _status(self) -> HostedHttpResponse:
        executors = self._actor.control_state.snapshot_executors()
        sessions = self._actor.control_state.snapshot_sessions()
        online: list[str] = []
        for executor_id in executors:
            if await self._actor.executor_transport.is_online(str(executor_id)):
                online.append(str(executor_id))
        return HostedHttpResponse(
            200,
            {
                "executors": [
                    {
                        "executor_id": str(row.executor_id),
                        "name": row.name,
                        "online": str(row.executor_id) in online,
                        "revoked": row.revoked_at is not None,
                    }
                    for row in executors.values()
                ],
                "sessions": [
                    {
                        "session_id": str(row.session_id),
                        "executor_id": str(row.executor_id),
                        "status": row.status,
                        "workdir": row.resolved_workdir_display,
                    }
                    for row in sessions.values()
                ],
            },
        )


def _bearer(headers: Mapping[str, str]) -> str:
    header = headers.get("authorization") or headers.get("Authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return ""
    return value


def owner_bearer_matches(
    headers: Mapping[str, str],
    owner_token: str,
) -> bool:
    """Check the hosted owner bearer without constructing control state."""
    bearer = _bearer(headers)
    return bool(bearer) and hmac.compare_digest(bearer, owner_token)


def _pairing_view(view: _PairingAttemptView) -> dict[str, object]:
    return {
        "user_code": view.user_code,
        "requested_name": view.requested_name,
        "existing_executor_id": view.existing_executor_id,
        "metadata": view.metadata.model_dump(mode="json"),
        "expires_in": view.expires_in,
        "status": view.status,
        "executor_id": view.executor_id,
        "name": view.name,
    }


def _protocol_error(exc: Exception) -> ProtocolError | None:
    error = getattr(exc, "error", None)
    return error if isinstance(error, ProtocolError) else None


def _protocol_payload(error: ProtocolError) -> dict[str, object]:
    return ProtocolErrorResponse(error=error).model_dump(mode="json")


def _pairing_error(exc: Exception) -> HostedHttpResponse | None:
    error = _protocol_error(exc)
    if error is None:
        return None
    code = error.code
    if code is ProtocolErrorCode.PAIRING_PENDING:
        status = 202
    elif code is ProtocolErrorCode.PAIRING_DENIED:
        status = 403
    elif code is ProtocolErrorCode.PAIRING_EXPIRED:
        status = 410
    elif code is ProtocolErrorCode.PAIRING_REQUIRED:
        status = 404
    elif code is ProtocolErrorCode.PAIRING_CAPACITY_EXHAUSTED:
        status = 429
    else:
        status = 400
    return HostedHttpResponse(status, _protocol_payload(error))


def _transport_error(exc: Exception) -> HostedHttpResponse | None:
    error = _protocol_error(exc)
    if error is None:
        return None
    code = error.code
    if code is ProtocolErrorCode.UNAUTHORIZED_EXECUTOR:
        status = 401
    elif code is ProtocolErrorCode.EXECUTOR_REVOKED:
        status = 403
    elif code is ProtocolErrorCode.UNKNOWN_COMMAND:
        status = 404
    elif code is ProtocolErrorCode.EXECUTOR_OVERLOADED:
        status = 409
    else:
        status = 400
    return HostedHttpResponse(status, _protocol_payload(error))


def _unauthorized() -> HostedHttpResponse:
    return HostedHttpResponse(
        401,
        {"detail": "owner bearer required"},
    )


_PAIR_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Workgate executor pairing</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font:16px system-ui;max-width:38rem;margin:3rem auto;padding:0 1rem}
label{display:block;margin:1rem 0 .35rem}input,button{font:inherit;padding:.55rem}
input[type=text],input[type=password]{box-sizing:border-box;width:100%}
button{margin:.75rem .5rem 0 0}.actions{display:flex;gap:.5rem}
dl{display:grid;grid-template-columns:10rem 1fr;gap:.35rem 1rem}
dt{font-weight:600}dd{margin:0;overflow-wrap:anywhere}pre{white-space:pre-wrap}
#replaceWrap{display:flex;gap:.5rem;align-items:center}#replaceWrap input{width:auto}
</style>
<h1>Pair an executor</h1>
<p>Enter the owner token and the code shown by <code>workgate executor connect</code>.</p>
<label>Owner token</label><input id="token" type="password" autocomplete="off">
<label>User code</label><input id="code" type="text" autocomplete="off">
<button id="review">Review request</button>
<section id="details" hidden>
<h2>Request</h2>
<dl>
<dt>Requested name</dt><dd id="requestedName">—</dd>
<dt>Hostname</dt><dd id="hostname">—</dd>
<dt>Platform</dt><dd id="platform">—</dd>
<dt>Build</dt><dd id="build">—</dd>
<dt>Existing executor</dt><dd id="existing">None</dd>
<dt>Expires</dt><dd id="expiry">—</dd>
</dl>
<label>Approved name (optional)</label><input id="approvedName" type="text">
<label id="replaceWrap" hidden><input id="replace" type="checkbox">Replace the existing executor credential</label>
<div class="actions"><button id="approve">Approve</button><button id="deny">Deny</button></div>
</section>
<pre id="out"></pre>
<script>
let pairing=null;
const authHeaders=()=>({
  "authorization":"Bearer "+token.value,
  "content-type":"application/json"
});
async function post(path,body){
  const response=await fetch(path,{
    method:"POST",headers:authHeaders(),body:JSON.stringify(body)
  });
  const text=await response.text();
  let data=null;
  try{data=text?JSON.parse(text):null}catch(_){data=null}
  if(!response.ok){throw new Error(response.status+" "+(data?.detail||text))}
  return data;
}
review.onclick=async()=>{
  out.textContent="";details.hidden=true;pairing=null;
  try{
    pairing=await post("/pair/lookup",{
      user_code:code.value.trim().toUpperCase()
    });
    requestedName.textContent=pairing.requested_name||"Not requested";
    hostname.textContent=pairing.metadata?.hostname||"Not reported";
    platform.textContent=pairing.metadata?.platform||"Not reported";
    build.textContent=pairing.metadata?.build||"Not reported";
    existing.textContent=pairing.existing_executor_id||"None";
    expiry.textContent=(Number(pairing.expires_in)||0)+"s remaining";
    approvedName.value=pairing.requested_name||"";
    replace.checked=false;
    replaceWrap.hidden=!pairing.existing_executor_id;
    details.hidden=false;
  }catch(error){out.textContent=String(error)}
};
async function decide(decision){
  if(!pairing)return;
  const body={user_code:pairing.user_code,decision};
  if(decision==="approve"){
    const name=approvedName.value.trim();
    if(name)body.name=name;
    if(pairing.existing_executor_id&&replace.checked){
      body.replace_executor_id=pairing.existing_executor_id;
    }
  }
  try{
    const result=await post("/pair",body);
    out.textContent="200 "+JSON.stringify(result);
    details.hidden=true;pairing=null;
  }catch(error){out.textContent=String(error)}
}
approve.onclick=()=>decide("approve");
deny.onclick=()=>decide("deny");
</script>
"""
