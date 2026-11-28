"""Opt-in WorkGate-native Hosted Chat generation transport."""

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

import jwt
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from ...hosted_chat.approval_claims import ApprovalClaimStore
from ...hosted_chat.ask_user_tools import ASK_USER_QUESTION_TOOL_NAME
from ...hosted_chat.content_filters import ContentFilterBlocked
from ...hosted_chat.database import SQLiteDatabase
from ...hosted_chat.generation_protocol import request_protocol
from ...hosted_chat.generation_supervisor import GenerationCapacityExceeded
from ...hosted_chat.generations import (
    Generation,
    GenerationConflict,
    GenerationStore,
)
from ...oauth.core.context import current_oauth_claims
from ...oauth.core.scopes import SUPPORTED_OAUTH_SCOPE_SET, scope_set
from ...oauth.http.middleware import revalidate_browser_session
from ...oauth.protocol.token_codec import validate_bearer_token
from ...ui.http.routes import UI_API_PREFIX
from ..hosted_chat_generation_host import run_authenticated_generation
from ..hosted_chat_native_creation import _registered
from ..hosted_chat_native_dispatch import NATIVE_TOOLS
from ..runtime import ControlRuntime

_PREFIX = UI_API_PREFIX + "/chat/generations"
_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
_TERMINAL = {"complete", "error", "aborted", "interrupted", "requires_action"}


@dataclass(frozen=True)
class _Principal:
    owner: str
    scopes: frozenset[str]
    expires: int | None
    check: Callable[[], None]


def _principal(runtime: ControlRuntime, request: Request) -> _Principal:
    claims = current_oauth_claims()
    if claims is None:
        if runtime.config.auth_mode != "none":
            raise PermissionError(
                "Hosted Chat requires an authenticated principal"
            )
        return _Principal(
            "local-user", SUPPORTED_OAUTH_SCOPE_SET, None, lambda: None
        )
    original = dict(claims)
    owner = original.get("sub")
    expiry = original.get("exp")
    if not isinstance(owner, str) or not owner or type(expiry) is not int:
        raise PermissionError(
            "Hosted Chat requires a finite authenticated grant"
        )
    scopes = frozenset(
        scope_set(str(original.get("scope") or ""))
    ).intersection(SUPPORTED_OAUTH_SCOPE_SET)
    authorization = request.headers.get("authorization")
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise PermissionError("Invalid Hosted Chat credential")

        def read_bearer():
            return validate_bearer_token(token)

        read_claims = read_bearer
    else:

        def read_cookie():
            return revalidate_browser_session(request)

        read_claims = read_cookie

    def check():
        try:
            if expiry <= time.time() or read_claims() != original:
                raise PermissionError("Hosted Chat grant expired or changed")
        except (jwt.PyJWTError, ValueError, OSError) as exc:
            raise PermissionError(
                "Hosted Chat grant is no longer valid"
            ) from exc

    check()
    return _Principal(owner, scopes, expiry, check)


def _body(generation: Generation, *, protocol: int | None = None) -> dict:
    return {
        "runId": generation.run_id,
        "streamId": generation.run_id,
        "conversationId": generation.conversation_id,
        "generationCreatedAt": generation.generation_created_at,
        "generationProtocolVersion": generation.protocol_version
        if protocol is None
        else protocol,
        "status": generation.status,
    }


def hosted_chat_generation_routes(runtime: ControlRuntime) -> list[Route]:
    """Use the same protected /api/ui boundary as other native UI routes."""

    def store() -> GenerationStore:
        # No database creation on disabled or unauthenticated requests.
        return GenerationStore(SQLiteDatabase(runtime.config.state_dir))

    def owned(request: Request, principal: _Principal, repo: GenerationStore):
        run_id = request.path_params["run_id"]
        generation = repo.get(principal.owner, run_id)
        admission = repo.native_admission(principal.owner, run_id)
        if generation is None or admission is None:
            raise KeyError("Generation not found")
        return generation, admission

    def require_admission(principal: _Principal, admission: dict) -> set[str]:
        principal.check()
        scopes = set(admission["scopes"])
        if not scopes.issubset(principal.scopes):
            raise PermissionError(
                "Reattachment requires the admitted scope ceiling"
            )
        return scopes

    def launch(
        principal: _Principal,
        repo: GenerationStore,
        generation: Generation,
        admission: dict,
    ) -> bool:
        if generation.status != "running":
            return False
        scopes = require_admission(principal, admission)
        tools = (
            NATIVE_TOOLS | {ASK_USER_QUESTION_TOOL_NAME}
            if admission.get("ask_user") is True
            else NATIVE_TOOLS
        )

        async def authorize_batch(_calls, _state):
            # The native node applies P4 to every effective tool argument
            # and approval edit before execution. This callback owns the
            # current HTTP grant; it must not use persisted scopes as credentials.
            principal.check()

        async def execute():
            return await run_authenticated_generation(
                runtime,
                repo.database,
                principal.owner,
                generation.run_id,
                scopes=scopes,
                expires=principal.expires,
                allowed_tools=tools,
                task_machine=admission["task_machine"],
                check_grant=principal.check,
                authorize_batch=authorize_batch,
            )

        return runtime.generation_supervisor.start(
            principal.owner, generation.run_id, execute
        )

    def failure(error: Exception) -> JSONResponse:
        if isinstance(error, ContentFilterBlocked):
            return JSONResponse(
                error.response(), status_code=400, headers=_HEADERS
            )
        if isinstance(error, GenerationConflict):
            return JSONResponse(
                _body(error.existing), status_code=409, headers=_HEADERS
            )
        if isinstance(error, KeyError):
            return JSONResponse(
                {"error": "Generation or resource not found"},
                status_code=404,
                headers=_HEADERS,
            )
        if isinstance(error, PermissionError):
            return JSONResponse(
                {"error": "Hosted Chat authorization denied"},
                status_code=403,
                headers=_HEADERS,
            )
        return JSONResponse(
            {"error": "Invalid generation request"},
            status_code=400,
            headers=_HEADERS,
        )

    async def start(request: Request):
        try:
            principal = _principal(runtime, request)
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Expected object")
            machine = payload.get("taskMachine")
            if machine is not None and (
                not isinstance(machine, str)
                or machine not in _registered(runtime)
            ):
                raise ValueError("Unknown native Executor")
            repo = store()
            generation, fresh = repo.begin(
                principal.owner,
                payload,
                protocol_version=request_protocol(request, payload),
                native_admission={
                    "scopes": sorted(principal.scopes),
                    "task_machine": machine,
                },
            )
            admission = repo.native_admission(
                principal.owner, generation.run_id
            )
            if admission is None or admission["task_machine"] != machine:
                raise PermissionError("Generation native admission differs")
            try:
                launch(principal, repo, generation, admission)
            except GenerationCapacityExceeded:
                return JSONResponse(
                    {
                        **_body(generation),
                        "error": "Generation host capacity reached",
                    },
                    status_code=429,
                    headers=_HEADERS,
                )
            protocol = request_protocol(
                request, payload, stored_version=generation.protocol_version
            )
            return JSONResponse(
                {**_body(generation, protocol=protocol), "resumed": not fresh},
                status_code=202,
                headers=_HEADERS,
            )
        except (
            KeyError,
            ValueError,
            TypeError,
            PermissionError,
            GenerationConflict,
        ) as error:
            return failure(error)

    async def attach(request: Request):
        try:
            principal = _principal(runtime, request)
            repo = store()
            generation, admission = owned(request, principal, repo)
            try:
                launch(principal, repo, generation, admission)
            except GenerationCapacityExceeded:
                return JSONResponse(
                    {
                        **_body(generation),
                        "error": "Generation host capacity reached",
                    },
                    status_code=429,
                    headers=_HEADERS,
                )
            return JSONResponse(
                _body(generation), status_code=202, headers=_HEADERS
            )
        except (KeyError, ValueError, PermissionError) as error:
            return failure(error)

    async def status(request: Request):
        try:
            principal = _principal(runtime, request)
            generation, _ = owned(request, principal, store())
            return JSONResponse(_body(generation), headers=_HEADERS)
        except (KeyError, ValueError, PermissionError) as error:
            return failure(error)

    async def pending_action(request: Request):
        try:
            principal = _principal(runtime, request)
            repo = store()
            generation, admission = owned(request, principal, repo)
            require_admission(principal, admission)
            pending = ApprovalClaimStore(repo.database).pending(
                principal.owner, generation.run_id
            )
            return JSONResponse({"pendingAction": pending}, headers=_HEADERS)
        except (KeyError, ValueError, PermissionError) as error:
            return failure(error)

    async def resolve_action(request: Request):
        try:
            principal = _principal(runtime, request)
            repo = store()
            generation, admission = owned(request, principal, repo)
            require_admission(principal, admission)
            payload = await request.json()
            if not isinstance(payload, dict) or not isinstance(
                payload.get("actionId"), str
            ):
                raise ValueError("Expected action identity")
            if generation.status != "requires_action":
                raise KeyError("No pending human action")
            if not await runtime.generation_supervisor.wait_idle(
                principal.owner, generation.run_id
            ):
                return JSONResponse(
                    {
                        "error": "Generation is still suspending; retry the resolution"
                    },
                    status_code=409,
                    headers=_HEADERS,
                )
            require_admission(principal, admission)
            claims = ApprovalClaimStore(repo.database)
            if payload.get("type") == "tool_approval":
                resolutions = payload.get("resolutions")
                if not isinstance(resolutions, list):
                    raise ValueError("Expected approval resolutions")
                claims.claim(
                    principal.owner,
                    generation.run_id,
                    payload["actionId"],
                    resolutions,
                )
            elif payload.get("type") == ASK_USER_QUESTION_TOOL_NAME:
                claims.claim_question(
                    principal.owner,
                    generation.run_id,
                    payload["actionId"],
                    payload,
                )
            else:
                raise ValueError("Unknown human action type")
            current = repo.get(principal.owner, generation.run_id)
            if current is None:
                raise KeyError("Generation not found")
            try:
                launch(principal, repo, current, admission)
            except GenerationCapacityExceeded:
                # Resolution is committed exactly once. Explicit attach can
                # resume it when capacity is available; do not replay a claim.
                return JSONResponse(
                    {
                        **_body(current),
                        "resolved": True,
                        "error": "Generation host capacity reached; attach to resume",
                    },
                    status_code=429,
                    headers=_HEADERS,
                )
            return JSONResponse(
                {**_body(current), "resolved": True},
                status_code=202,
                headers=_HEADERS,
            )
        except (KeyError, ValueError, TypeError, PermissionError) as error:
            return failure(error)

    async def events(request: Request):
        try:
            principal = _principal(runtime, request)
            repo = store()
            generation, _ = owned(request, principal, repo)
            after = int(
                request.query_params.get(
                    "after", request.headers.get("last-event-id", "0")
                )
            )
            if after < 0 or after > 2**63 - 1:
                raise ValueError("Invalid SSE cursor")
            protocol = request_protocol(
                request, stored_version=generation.protocol_version
            )
        except (KeyError, ValueError, PermissionError) as error:
            return failure(error)

        async def stream():
            cursor = after
            heartbeat = time.monotonic()
            try:
                while True:
                    principal.check()
                    batch = repo.events(
                        principal.owner, generation.run_id, cursor
                    )
                    for seq, event in batch:
                        principal.check()
                        cursor = seq
                        yield f"id: {seq}\ndata: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
                    current = repo.get(principal.owner, generation.run_id)
                    if current is None or (
                        current.status in _TERMINAL and not batch
                    ):
                        return
                    if time.monotonic() - heartbeat >= 12:
                        yield ": heartbeat\n\n"
                        heartbeat = time.monotonic()
                    if not batch:
                        await asyncio.sleep(0.15)
            except PermissionError:
                return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                **_HEADERS,
                "X-LibreChat-Generation-Protocol": str(protocol),
            },
        )

    return [
        Route(_PREFIX, start, methods=["POST"]),
        Route(_PREFIX + "/{run_id}", status, methods=["GET"]),
        Route(_PREFIX + "/{run_id}/attach", attach, methods=["POST"]),
        Route(_PREFIX + "/{run_id}/events", events, methods=["GET"]),
        Route(_PREFIX + "/{run_id}/action", pending_action, methods=["GET"]),
        Route(_PREFIX + "/{run_id}/action", resolve_action, methods=["POST"]),
    ]
